# Celery 异步任务（admin-backend + celeryworker）

admin-backend 与 celeryworker **共用同一镜像**：API 负责投递任务（producer），celeryworker Deployment 负责消费（worker）。应用代码**只认一个 broker 环境变量** `CELERY_BROKER_URL`；RabbitMQ 的 producer / worker 账号在 k8s 生成层按 Deployment 分别注入。

---

## 架构概览

```text
┌─────────────────────┐     CELERY_BROKER_URL (producer)      ┌──────────────┐
│  admin-backend API  │ ──────────────────────────────────────►│   RabbitMQ   │
│  POST /api/...      │     CELERY_QUEUE (ConfigMap)           │   vhost      │
└─────────────────────┘                                        └──────┬───────┘
                                                                        │
┌─────────────────────┐     CELERY_BROKER_URL (worker)                 │
│  celeryworker       │ ◄──────────────────────────────────────────────┘
│  uv run celery ...  │     CELERY_QUEUE (ConfigMap)
└─────────────────────┘
```

| 组件 | 镜像 | Broker 账号 | 权限 |
|------|------|-------------|------|
| admin-backend API | `{app}-admin-backend` | `{app}-admin-backend-producer` | 仅 publish |
| celeryworker | 同上 | `{app}-admin-backend-worker` | consume / ack |

vhost、用户、队列定义见 `messaging-platform/rabbitmq` 的 app definitions。

---

## 环境变量约定（应用层）

应用（`core/config.py`、`app/worker.py`）**只读取以下变量**，不识别 `RABBITMQ_PRODUCER_URL` 等别名。

| 变量 | 存放位置 | 说明 |
|------|----------|------|
| `CELERY_BROKER_URL` | Secret | AMQP 连接串；API 与 Worker 变量名相同，**值不同**（见 k8s 一节） |
| `CELERY_QUEUE` | ConfigMap | 默认队列名，须与 celeryworker 及 RabbitMQ definitions 一致 |
| `CELERY_RESULT_BACKEND` | Secret（可选） | 结果后端；默认不启用 |
| `CELERY_APP_MODULE` | celeryworker ConfigMap | Worker 启动模块，默认 `app.worker` |

本地开发可在 `.env` 中设置：

```bash
CELERY_BROKER_URL=amqp://tpl-admin-backend-producer:pass@localhost:5672/tpl-development
CELERY_QUEUE=tpl.admin.default
```

未设置 `CELERY_BROKER_URL` 时 API 仍可启动；调用 producer 或访问 `/api/internal/tasks/ping` 会返回 503。

---

## k8s 注入规则（方案 D）

**变量名统一为 `CELERY_BROKER_URL`，按 Deployment 注入不同 RabbitMQ 账号。**

### admin-backend API

| 资源 | 键 | 值来源 |
|------|-----|--------|
| `{app}-admin-backend-secret` | `CELERY_BROKER_URL` | producer 用户（generate 脚本由 `RABBITMQ_PRODUCER_USER/PASSWORD` 拼装） |
| `{app}-admin-backend-config` | `CELERY_QUEUE` | 如 `tpl.admin.default` |

API Pod **仅**挂载 admin-backend 的 ConfigMap/Secret，只会看到 producer 的 broker URL。

### celeryworker

| 资源 | 键 | 值来源 |
|------|-----|--------|
| `{app}-admin-backend-config` / `-secret` | 业务配置 | 与 API 相同（含 producer 的 `CELERY_BROKER_URL`） |
| `celeryworker-{app}-admin-backend-secret` | `CELERY_BROKER_URL` | worker 用户（**覆盖**上文 producer URL） |
| `celeryworker-{app}-admin-backend-config` | `CELERY_QUEUE` 等 | Worker 运行参数 |

Worker Pod 的 `envFrom` 顺序（后者覆盖同名键）：

1. `{app}-admin-backend-config`
2. `{app}-admin-backend-secret` → `CELERY_BROKER_URL` = producer
3. `celeryworker-*-config`
4. `celeryworker-*-secret` → `CELERY_BROKER_URL` = **worker（生效）**

因此 Worker 始终用 worker 账号消费，无需在 Python 中做 fallback。

---

## 代码结构

```text
app/
├── worker.py                          # Celery 实例，celeryworker 入口 -A app.worker
├── tasks/
│   ├── __init__.py
│   └── ping.py                        # 示例任务
└── infrastructure/messaging/
    └── celery_producer.py             # API 侧投递封装
```

### 定义任务

```python
# app/tasks/my_task.py
from app.worker import celery_app

@celery_app.task(name="app.tasks.my_task")
def my_task(payload: dict) -> str:
    ...
```

在 `app/tasks/__init__.py` 或 worker 末尾 import 以注册任务。

### API 投递任务

```python
from app.infrastructure.messaging.celery_producer import get_celery_producer
from core.config import get_settings

producer = get_celery_producer()
if not producer.enabled:
    ...  # Celery 未配置

# 示例 ping
task_id = producer.dispatch_ping()

# 或直接使用 task
from app.tasks.my_task import my_task
result = my_task.apply_async(
    args=[{"key": "value"}],
    queue=get_settings().celery_queue,
)
task_id = result.id
```

### 联调端点

```http
POST /api/internal/tasks/ping
```

响应示例：

```json
{"task_id": "...", "queue": "tpl.admin.default"}
```

Worker 日志中应出现对 `app.tasks.ping` 的消费记录。

---

## 部署与验证

### 1. 生成 Secret / ConfigMap

```bash
# admin-backend
bash .../generate-{app}-admin-backend-secret/generate-{app}-admin-backend-secret.sh
bash .../generate-{app}-admin-backend-config/generate-{app}-admin-backend-config.sh

# celeryworker
bash .../generate-celeryworker-{app}-admin-backend-secret/...
bash .../generate-celeryworker-{app}-admin-backend-config/...
bash .../generate-app/generate-app.sh
```

确认 admin-backend Secret 含 `CELERY_BROKER_URL`（producer），celeryworker Secret 含 `CELERY_BROKER_URL`（worker）。

### 2. 构建镜像

admin-backend 与 celeryworker 使用同一镜像标签，例如：

```bash
cd {app}-admin-backend/mybuild
CLUSTER=C1 ./build-image.sh && ./push-image.sh
```

### 3. 部署

按 deploy 脚本 apply admin-backend 与 celeryworker。

### 4. 端到端检查

```bash
# API 启动日志
kubectl logs deploy/{app}-admin-backend | grep "Celery producer"

# 投递 ping
curl -X POST https://{app}-admin-api.../api/internal/tasks/ping

# Worker 消费
kubectl logs deploy/celeryworker-{app}-admin-backend | grep ping
```

---

## 与 web-backend 的区别

| 服务 | Celery | Broker 变量 |
|------|--------|-------------|
| admin-backend + celeryworker | 是 | `CELERY_BROKER_URL` |
| web-backend | 否（仅 RabbitMQ 直发等） | 仍用 `RABBITMQ_PRODUCER_URL` |

admin-backend 走 Celery 生态；web-backend 不受本文档约束。

---

## 常见问题

**Q: 为何 API 和 Worker 用同一变量名？**  
A: 应用层只维护 Celery 标准配置；账号差异由 k8s 按 Pod 注入，避免 Python fallback 与双命名。

**Q: Worker 会误用 producer 账号吗？**  
A: 不会。celeryworker Secret 在 envFrom 中位于 admin-backend Secret 之后，同名 `CELERY_BROKER_URL` 被 worker URL 覆盖。

**Q: 本地只测 API、不启 Worker？**  
A: 可以。设置 producer 的 `CELERY_BROKER_URL` 后任务会进入队列；无 Worker 时消息堆积，属预期行为。

**Q: 新增业务任务要注意什么？**  
A: 任务模块需被 import；投递时使用 `get_settings().celery_queue`；RabbitMQ definitions 中队列须已声明。

---

## 相关路径

| 说明 | 模板 / 实例 |
|------|-------------|
| 应用代码 | `tpl-app/tpl-admin-backend/app/` |
| celeryworker 模板 | `tpl-app/celeryworker-tpl-admin-backend/` |
| k8s 实例 | `k8s/.../llm-app/`、`investment-app/`、`tools-app/` |

实例化后 `tpl` 替换为 `llm`、`investment`、`tools` 等应用名。
