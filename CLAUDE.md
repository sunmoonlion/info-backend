# info-backend — 局部编码规则

> 进入本目录时自动叠加。**本文件只约束局部编码。**
> 项目全貌见 `../../k8s/sunmoonai/docs/project-guide/overall-architecture.md`；
> 本仓细节见 `../../k8s/sunmoonai/docs/project-guide/repos/info-app.md`。
> **改代码前先读 `../../k8s/sunmoonai/docs/dev-plan/constraints.md`**（39 条，按主题分组）。
> 与代码冲突时以代码为准。

## 技术栈

Python ≥3.12 · FastAPI · async SQLAlchemy · Alembic · pydantic-settings ·
Redis（会话）· Celery（异步任务）· uv（依赖，锁文件 `uv.lock`）

## 进程入口：四角色一镜像

**同一个镜像按不同命令启动四种进程**，入口都在 `app/app/bootstrap/`：

| 角色 | 入口 |
| --- | --- |
| API | `app/bootstrap/api.py` — `create_app()` 工厂在这里 |
| Worker | `app/bootstrap/worker.py`（Celery） |
| Scheduler | `app/bootstrap/scheduler.py`（Celery beat） |
| Migration | `app/bootstrap/migration.py`（一次性） |

⚠ `app/app/main.py` 只有 5 行，是向后兼容的 ASGI 转发，**不是真正的入口**，不要改它。

## 目录结构

```
app/
├── core/config.py          Pydantic Settings + 生产期硬校验（配错则进程起不来）
├── app/bootstrap/          四个运行角色入口
├── app/domain/             领域模型、状态机、命令
├── app/application/        服务编排、DTO、ports（接口定义）
├── app/infrastructure/     ORM、外部适配、Celery、存储（ports 实现）
├── app/interfaces/
│   ├── http/               模板面：admin/web 认证、diagnostics、web interaction
│   ├── endpoints/          领域面：本仓自己的业务路由
│   ├── schemas/            请求/响应 schema
│   └── errors/             统一 problem+json 处理
├── app/tasks/              Celery 任务
├── alembic/versions/       迁移链（单链线性，见下）
└── tests/                  含 test_kernel_invariants.py
```

**`interfaces/http/` 与 `interfaces/endpoints/` 的分工**：模板提供的通用面在 `http/`，
本仓的领域路由在 `endpoints/`。加业务接口去 `endpoints/`。

## 会让你失败的规则

| 规则 | 后果 |
| --- | --- |
| `app/application/` 不得出现 `app.interfaces` 字符串 | `test_kernel_invariants` 失败 |
| 改迁移必须同步改 `tests/test_kernel_invariants.py` 里的文件名清单 | 测试失败（清单是逐字比对的） |
| 迁移链必须单链线性，恰好一个 `down_revision = None` | 测试失败 |
| `pyproject.toml` 与 `uv.lock` 的 version 必须同为 `2.0.0` | 测试主动断言，须与正式发布别名一致 |
| 凭据一律经 pydantic-settings 从环境变量读 | 生产期配置校验会拒绝 |
| `.dockerignore` 须排除 `app/.env`、`app/.env.*`、`app/tests` | 测试失败 |

**配置错误的表现是启动抛异常，不是运行期降级**——`core/config.py` 有约 35 处生产期
硬校验。改配置后先起一次进程验证。

## 三件套

```bash
cd app
uv sync --frozen
uv run ruff check .
uv run pyright
uv run pytest -q
```

## 动手前

1. 读 `app/interfaces/endpoints/` 与 `app/interfaces/http/` 确认现有路由
2. 读 `app/infrastructure/models/` 确认数据字段
3. 改模型必须生成 Alembic 迁移，不手改表结构
4. 跨 App 契约的 schema 真源在 provider 仓，本仓若是 consumer 只改锁文件，
   且必须双端测试通过——见 `../../k8s/sunmoonai/docs/project-guide/topics/contracts.md`
