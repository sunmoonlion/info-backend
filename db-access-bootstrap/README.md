# db-access-bootstrap (admin-backend)

用于 `tpl-admin-backend` 的数据库接入脚手架。

- external: `setup-external-db-access.sh`
- k8s: `setup-k8s-db-access.sh`
- 回收: `teardown-*.sh`

默认启用 PostgreSQL + MongoDB，Redis 默认关闭（可在 `config/common.env` 打开）。

占位规则与 `init.sh` 一致：保留 `tpl` 作为替换锚点，初始化项目时会统一替换为真实项目名。
