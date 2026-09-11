# Info Backend 异步任务与运行角色

`info-backend` 的 API、Worker、Scheduler、Migration 来自同一源码和不可变镜像，分别通过 `app.bootstrap.api`、`app.bootstrap.worker`、`app.bootstrap.scheduler`、`app.bootstrap.migration` 启动。

Worker 不是独立源码项目。当前领域任务包括抓取、索引和向 Knowledge 分发，分别由 `delivery_handlers.py` 注册。API 在领域事务中写入 `outbox_message`，不直接调用 Celery 或在 broker 不可用时回退到进程内执行。Scheduler 每 5 秒触发公共 `durable_delivery.pump`，Worker 发布已持久化的命令并执行 `durable_delivery.execute`；broker 消息只携带消息 UUID。

发布成功不是执行回执。公共投递层负责租约、有限重试、死信、重放与周期性对账；消费层在提交前校验执行租约并写入 Inbox。外部副作用的幂等仍由领域处理器负责。索引重建接口只返回入队数量，不表示索引已完成。旧任务入口显式拒绝执行，不能与新 Worker 混用。

各角色必须使用不同 ServiceAccount、Secret、资源与伸缩策略。部署材料位于并列 `k8s` 仓的 `sunmoonai/app-platform/info-app/deployment/`；现存正式部署材料不等于本轮候选源码已经发布。旧 v1 运行资源只属于回滚拓扑，观察窗结束前不得直接删除。

迁移顺序、旧任务切换、回滚限制与运维命令见 [可靠投递说明](durable-delivery-luna.md)。
