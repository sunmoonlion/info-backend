# db-access-bootstrap template

用于给任意业务组件快速落地“外部数据库调用”脚本模式。

## 文件说明

- `setup-external-db-access.sh`：external 场景，创建/更新租户并生成运行时 env
- `teardown-external-db-access.sh`：external 场景，回收租户并删除运行时 env
- `setup-k8s-db-access.sh`：k8s 场景，创建/更新租户并写入 K8s Secret
- `teardown-k8s-db-access.sh`：k8s 场景，回收租户并删除 K8s Secret
- `config/common.env`：统一开关和路径
- `config/postgresql.external.env`：PostgreSQL 外部配置
- `config/postgresql.k8s.env`：PostgreSQL k8s 配置
- `config/redis.external.env`：Redis 外部配置
- `config/redis.k8s.env`：Redis k8s 配置
- `config/mongodb.external.env`：MongoDB 外部配置
- `config/mongodb.k8s.env`：MongoDB k8s 配置

## 默认行为

- PostgreSQL：通过 `dbctl` 幂等创建/授权，写入 `DATABASE_URL`
- Redis：做外部连通校验，写入 `REDIS_HOST/REDIS_PORT/REDIS_DB/REDIS_PASSWORD`
- MongoDB（可选）：通过 `dbctl` 幂等创建/授权，写入 `MONGODB_URI`

## 典型使用

```bash
./setup-external-db-access.sh
set -a && source ./.env.c1-external && set +a
```

## 回收

```bash
./teardown-external-db-access.sh
```

## k8s 场景

```bash
./setup-k8s-db-access.sh
```

```bash
./teardown-k8s-db-access.sh
```

## 安全提醒（非常重要）

- **不要把生产密码写进 repo**：`config/*.env` 中出现的 `change_me`/示例密码仅用于演示。
- **k8s 场景推荐**：通过 Secret/密管把敏感变量注入到执行环境中（例如 `PG_ADMIN_PASSWORD`、`APP_DB_PASSWORD`、`REDIS_PASSWORD`），让 `.env` 文件只保存非敏感配置。
- **Redis 要求**：
  - 若业务服务不支持 Redis ACL username，请使用 `REDIS_AUTH_ONLY=true`（只做密码认证与写 Secret，不创建 ACL 用户）。
  - 执行机必须使用支持 `--user` 的 `redis-cli`（Redis CLI >= 6）。
