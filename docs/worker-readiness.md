# Worker 消费配置 readiness

在 Worker 容器相同环境执行 `python -m app.cli.worker_readiness`。`POD_NAME` 必填，
启动 Worker 时须使用 `--hostname="celery@${POD_NAME}"`。成功退出 0、无输出；失败
退出 1、stderr 仅 `worker_not_ready`。不接受任意目标、broker URL 或执行命令参数。

从 `app.bootstrap.worker` 装配真实 Settings/Celery app。定向本节点查询 active_queues
及 registered，必须恰好消费一个已配置的持久队列，交换机/路由/持久性正确，且镜像中
`app.tasks.*` 全部已注册（含实例领域扩展）。不缓存成功，不 grep 文本，不发送业务测试任务。
空/错误节点/错误结构/缺任务/错队列/配置异常/无回包/连接错误均失败关闭。

命令通过标准库子进程隔离 broker 连接及重试：子进程预算 6 秒，两个控制请求各等 1 秒，
超时 kill + wait；库日志丢弃，不将 DSN/堆栈打印到探针事件。Kubernetes timeoutSeconds
仍为 8 秒，保留解释器启动/回收余量；OS 进程创建与极端系统停顿不构成硬实时保证。
`--check-local` 是受外层截止保护的内部子进程入口，不可单独配置为部署探针。

这是 broker 控制面与消费配置就绪，不是业务进展、DB readiness 或 Scheduler 心跳。
控制请求会产生短期回复队列及控制消息，需要现有 Worker 身份的相关 broker 权限；
不增加权限、不取消/新增消费者、不读业务 DB、不 claim/replay/GC。没有 HTTP 暴露面。
短暂断连可能使 Pod NotReady；**不得直接用作 liveness**，避免 broker 故障导致重启风暴。
NotReady 不会停止 Celery 继续消费；池子进程卡住或业务失败也可能保持 Ready。
节点名必须唯一；单次返回未见重复不能证明集群中绝无同名节点。

依据 [Celery Worker 官方说明](https://docs.celeryq.dev/en/main/userguide/workers.html)：
控制回复有超时，solo 执行任务时可能阻塞控制请求。此模板默认 prefork；本包不承诺
solo/其它 broker/pool 组合已验收。队列/注册列表是两次非原子快照，配置突变需下一次复核。

模板脚手架为未来 bundle 接线。现有实例 release/bundle 锁着旧镜像，禁止只换探针命令
冒充新源码已发布；下次构建/发布时将新镜像 digest、探针及 release 哈希一起生成、验收。
业务 ACL/网络、真实负载、消费进展及 Scheduler/采集告警仍须独立验收。

测试：常规 `pytest` 含单元与可选真实 RabbitMQ 用例。完整验收必须设置
`CELERY_PROBE_TEST_BROKER_URL=amqp://<test-user>:<test-password>@127.0.0.1:<port>/luna_probe_tests`
至一次性独立 broker；仅 loopback + 固定测试 vhost 可用。测试启动本仓真实 prefork Worker，
仅在随机测试队列模拟取消/恢复消费者，结束终止自身 Worker，不连接业务环境。
