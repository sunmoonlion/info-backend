# Info Backend 异步任务与运行角色

`info-backend` 的 API、Worker、Scheduler、Migration 来自同一源码和不可变镜像，分别通过 `app.bootstrap.api`、`app.bootstrap.worker`、`app.bootstrap.scheduler`、`app.bootstrap.migration` 启动。

Worker 不是独立源码项目。当前领域任务包括抓取、搜索和向 Knowledge 分发；API 仅持有 producer 权限，Worker 持有对应队列的 consumer 权限，Scheduler 只获得周期投递权限。各角色必须使用不同 ServiceAccount、Secret、资源与伸缩策略。

新 K8s 资源由 `tpl-app/k8s-scaffold-v2` 的统一运行角色模型生成。旧 `celeryworker-info-admin-backend` 与 `nodebullworker-info-web-backend` 只属于 v1 回滚拓扑，在 R5/R7 门禁前保留，但不得作为新源码或新部署生成器。
