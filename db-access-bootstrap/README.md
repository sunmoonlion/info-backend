# db-access-bootstrap (admin-backend)

用于 `tpl-admin-backend` 的数据库接入脚手架（与 `tpl-web-backend/db-access-bootstrap` 结构对齐）。

**执行环境**：Git Bash（Windows）或 Linux/macOS 的 `bash`（脚本依赖 `sed`、`grep` 等）。

---

## 目录与产物

| 路径 | 作用 |
|------|------|
| `../db-provisioner/` | 本 backend 自带的 **`dbctl`**（`DBCTL_BIN` 默认指向此处）；admin/web 各一份，独立维护 |
| `config/common.env` | 总开关、`DBCTL_BIN`、`OUT_ENV`、各 engine 的 config 路径 |
| `config/postgresql.external.env` | 集群**外** PG：`dbctl` 变量（host、库、应用用户、管理员等） |
| `config/redis.external.env` | 集群**外** Redis：ACL 用户、`REDIS_KEY_PREFIX`、`+@connection` 等 |
| `config/*.k8s.env` | `dbctl --target k8s` 下发 Secret |
| `config/base.env.template` | `OUT_ENV` 初始模板 |
| **`.env.local.db`** | **`OUT_ENV` 默认路径**；`setup-external` 后含 `DATABASE_URL`、`SQLALCHEMY_DATABASE_URI`、`REDIS_*`（**敏感**） |
| **`.env.reference`** | `merge-and-generate-app-env.sh` 从 OUT 抽取的参考片段（**敏感**；见 `.gitignore`） |
| `../app/.env` | FastAPI 运行时读取；**合并只改库/Redis 相关键**，Casdoor、FRONTEND 等保留 |

---

## 前置条件

1. **`db-provisioner`**：与本目录同级的 **`../db-provisioner/bin/dbctl`** 默认可用（admin 自带副本，与 web 解耦）；首次可 `chmod +x ../db-provisioner/bin/dbctl`。若 `dbctl` 在别处，导出 **`DBCTL_BIN`** 覆盖。
2. **`external`**：本机可达 PG/Redis；通常需 **`psql`**（脚本内会校验）。
3. **`k8s`**：本机 **`kubectl`** 可用，`NAMESPACE` 等与集群一致。
4. **`../app/.env` 已存在**（可从 `../app/.env.example` 复制并补 Casdoor）。

---

## `config/common.env` 要点

- **`ENABLE_POSTGRESQL` / `ENABLE_REDIS` / `ENABLE_MONGODB`**：管理端不用 Mongo，一般 **`ENABLE_MONGODB=false`**。
- **`OUT_ENV`**：默认 **`${SCRIPT_DIR}/.env.local.db`**。
- **`DBCTL_BIN`**：未设置时解析为 **`tpl-admin-backend/db-provisioner/bin/dbctl`**（相对 `db-access-bootstrap` 的上一级）；使用仓库外二进制时用环境变量覆盖（Windows 可用 `/c/...`）。

---

## 主入口：`merge-and-generate-app-env.sh`

一条命令：**（可选）provision → 合并 `../app/.env` → 写 `.env.reference`**。

```bash
cd tpl-admin-backend/db-access-bootstrap
chmod +x merge-and-generate-app-env.sh setup-external-db-access.sh setup-k8s-db-access.sh   # 如需

./merge-and-generate-app-env.sh              # 默认 = external
./merge-and-generate-app-env.sh external
./merge-and-generate-app-env.sh k8s
./merge-and-generate-app-env.sh merge-only
./merge-and-generate-app-env.sh -h
```

| 参数 | 说明 |
|------|------|
| **`external`**（默认） | 先 **`setup-external-db-access.sh`**（`dbctl` + 写 `.env.local.db`），再合并，再 **`.env.reference`**。 |
| **`k8s`** | 先 **`setup-k8s-db-access.sh`**，再合并 + `.env.reference`。**不**新建 `.env.local.db`；不存在则失败。 |
| **`merge-only`** | 不跑 `setup-*`，只按已有 `.env.local.db` 合并并写 `.env.reference`。 |
| **`-h` / `--help` / `help`** | 打印用法。 |

**说明**：`setup-k8s-db-access.sh` **只**下发集群 Secret，**不**生成 `.env.local.db`。要用 `k8s` 后再合并，须先 **`external`** 或自备 `OUT_ENV` 文件。

### 合并进 `../app/.env` 的键

从 **`OUT_ENV`** 读取并覆盖/追加：

`SQLALCHEMY_DATABASE_URI`，`REDIS_HOST`、`REDIS_PORT`、`REDIS_DB`、`REDIS_PASSWORD`、`REDIS_USER`。

若 **OUT 无 `REDIS_USER=`**，会**删除** `app/.env` 里原有 `REDIS_USER=`（使用 Redis default 用户）。

**不改**：`CASDOOR_*`、`FRONTEND_BASE_URL`、`SESSION_*`、`ENV`、`LOG_LEVEL` 等。

### `.env.reference`

写入 OUT 中的：`APP_ENV`、`DATABASE_URL`、`SQLALCHEMY_DATABASE_URI`、所有 **`REDIS_`** 行（含 `REDIS_URI`）。**勿提交公开仓库。**

---

## 其它脚本

- 单独 provision：`setup-external-db-access.sh`、`setup-k8s-db-access.sh`
- 回收：`teardown-*.sh`
- **`merge-to-app-env.sh`**：兼容封装，等价 **`merge-and-generate-app-env.sh merge-only`**

`setup-external-db-access.sh` 会写 **`SQLALCHEMY_DATABASE_URI=postgresql+asyncpg://...`**（与 `../app/core/config.py` 一致）。Redis ACL 需在 `app/.env` 配 **`REDIS_USER`** + **`REDIS_PASSWORD`**（应用已支持可选 `redis_user`）。

---

## `external` 与 `k8s` 配合

1. 首次在能连实例的机器上：**`merge-and-generate-app-env.sh external`**。
2. 仅更新集群 Secret：**`merge-and-generate-app-env.sh k8s`**（须已有 `.env.local.db`）。
3. 只改了 `.env.local.db`：**`merge-only`**。

---

## 常见问题

- **`k8s` 报 missing OUT**：先 **`external`** 或自备 `.env.local.db`。
- **合并后 Redis 失败**：OUT 与实例 ACL/密码一致；改 `redis.external.env` 后重新 **`external`** 或改 OUT 后 **`merge-only`**。
- **Windows**：用 **Git Bash**；仅在使用**仓库外**的 `dbctl` 时，再把 **`DBCTL_BIN`** 设为 `/c/...` 等形式。

---

## 推荐流程（集群外）

1. 编辑 `config/postgresql.external.env`、`redis.external.env`、`common.env`。
2. `./merge-and-generate-app-env.sh external`
3. 检查 `../app/.env`（PG/Redis + Casdoor）。
4. 重启：`uv run uvicorn app.main:app --host 0.0.0.0 --port 8001`

默认 **PostgreSQL + Redis 开启**，**MongoDB 关闭**。占位规则与 `init.sh` 一致（保留 `tpl` 锚点）。