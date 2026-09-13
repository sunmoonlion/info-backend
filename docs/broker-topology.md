# 预建任务拓扑与分角色 RabbitMQ 权限

`CELERY_TASK_TOPOLOGY_PREDECLARED` 默认 `false`，保持既有自动声明行为。
本开关只提供运行时适配；不创建账号、不更改 Secret、不执行供给或现有集群切换。
当前候选验收范围是 AMQP 0-9-1 / RabbitMQ 4.1.3、未配置 Celery result backend。

## 为什么需要显式模式

当前任务交换机与队列同名，均取 `CELERY_QUEUE`。Kombu 自动 queue.bind 需要交换机
read，而 RabbitMQ 资源权限按名称匹配；给同名交换机 read 也让该账号可以消费队列。
因此仅改账号或 Secret 键，不足以让 API / Scheduler 成为只发布的角色。

启用本开关前，供给面必须先校验并建立唯一的 durable direct 交换机、durable 非排他且
非 auto-delete 的队列，以及同名 routing key 的精确绑定。拓扑供给不由运行进程承担。

启用后：

- 任务 Queue 使用 `no_declare=True`；Worker 与生产者都不再自动创建/绑定任务拓扑。
- 禁止自动创建未知队列，拼错目标必须失败。
- AMQP 开启 publisher confirms；默认路由（含 Celery 内置任务）使用 `mandatory=True`
  和 5 秒确认等待。漏建交换机、队列或绑定，实际 publish 抛错，不记为正常返回。
- 保留原交换机、队列、routing key、序列化及 late ack 配置；控制/回复/事件资源仍由
  Celery 管理，未关闭 remote control、gossip、mingle、heartbeat 或 readiness。

publisher confirm / 可路由不等于 Worker 成功执行，更不等于领域回执已提交。
确认丢失仍可能导致重复投递；原 Outbox/Inbox、租约、fencing 和领域幂等继续负责。
调用方显式覆盖路由选项或自行使用底层 AMQP 不属于这个默认路由保证，运行代码仍受信任。

## 权限与剩余边界

API、Scheduler 分别用独立 broker 用户，只 write 精确任务交换机，不授 configure/read。
Worker 要消费及再次发布任务，还要使用本 vhost 的 Celery 控制/回复/事件资源。
Migration 不需要 broker 用户。模板父仓 `k8s-deployment/runtime_broker_policy.py` 是
纯 topology/permissions 编译器；凭据和用户创建不在其输出中。

Worker 获得队列 read 后也具有 purge 能力，read/write 组合也可能允许重新绑定；本策略
不是 Worker 内部防篡改或按消息内容授权。pidbox 权限不能区分 inspect 与 shutdown，
同一 vhost 的 Worker 控制面是信任域。不能称其为逐命令审批机制。

尚未接入业务供给/渲染：不得直接只开新开关或先撤旧权限。切换须联合验证实际拓扑、
启动 definitions、独立身份、消费者排空、旧连接撤销、备份恢复与源码/镜像/DB 基线。
回退自动声明模式也需要相应 configure/read 权限，不能仅回退布尔开关。
