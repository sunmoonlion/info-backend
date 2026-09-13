# Scheduler 本机活动观测（B7i）

Scheduler 入口仍是 `app.bootstrap.scheduler:celery_app`。此入口选用
`ObservedScheduler`，继承 Celery `PersistentScheduler`，不改任务清单、调度算法、
reserve/重试/持久化格式，也不另建业务账。其他自选入口或显式 `--scheduler` 覆盖不
保证有此观测，不能仅凭同一镜像推断它已启用。

正常 Beat 命令保持原样，例如：

```bash
celery -A app.bootstrap.scheduler:celery_app beat --loglevel=INFO --schedule=/tmp/celerybeat-schedule
```

在**同一 Scheduler 容器、UID 和 PID namespace**内只读查询：

```bash
python -m app.cli.scheduler_activity --schedule /tmp/celerybeat-schedule --max-age 30
```

`--schedule` 必须与 Beat 参数指向同一文件；`--max-age` 必须显式指定有限正数，
最多 3600 秒。示例 30 秒不是统一 SLA：须覆盖真实最大调度间隔、连接重试及正常暂停。
返回 0 和 JSON 只表明“所记录进程存在、未暂停/退出且最近一次循环返回仍在该年龄内”；
不表明业务就绪。读取/校验失败退出 1，stdout 空，仅输出 `scheduler_activity_unavailable`；
参数语法错误沿用 argparse。CLI 不联 broker、不读数据库、不取身份凭据、不发任务、
不重启任何进程；路径应是本机临时存储，不宣称对任意网络文件系统有强制 I/O 时限。

## 记录语义

每次父类 `tick()` **正常返回之后**累计 tick，最多每秒尝试原子写一次同目录的
`<schedule>.activity.json`，文件权限 0600。没有独立心跳线程：主循环卡住时不会有
另一个线程继续“报活”。原子替换不会让 reader 看到半份 JSON；旧文件保留不等于新鲜。
这份可丢弃观测不做 fsync 持久性承诺，不可用它代替 Celery schedule 或数据库账本。

| 输出 | 含义 |
| --- | --- |
| `schema_version` | 目前为 1 |
| `pid` | 快照对应的 Beat 进程 |
| `loop_age_seconds` | 最近记录的已返回 tick 距当前 Linux BOOTTIME 的年龄 |
| `tick_count` | 当前 Scheduler 实例已返回 tick 的次数，重启重置 |
| `publish_returns` | 父类 apply_async 正常返回次数，不是 broker publisher confirm、执行或业务完成 |
| `publish_errors` | 父类 apply_async 抛 Exception 的次数；可能是连接、发布或 schedule sync 等错误，不细分原因 |

Celery 的 apply_entry 会记录部分发送异常并继续：所以循环活动新鲜、错误次数增加
可以同时发生。没有发送任务的空转也会 tick；反之任务发送调用阻塞时活动会逐渐过期。
计数是进程本地观测，不是数据库持久 `_total`，不能跨重启累加成可靠业务统计。
多个 Scheduler 共享同一 schedule 文件本就不受支持，此探针不是单实例锁或选主机制。

Reader 限制 4096 字节，拒绝符号链接、非普通文件、非当前 UID 文件、坏 JSON/类型、
过时/未来时间。校验 Linux boot ID、`/proc/<pid>/stat` 的进程启动 ticks、当前进程状态，
防上次启动遗留文件/PID 复用/已暂停或退出的进程误报。BOOTTIME 包括系统 suspend，
不使用墙钟或文件 mtime，墙钟回拨不能刷新此年龄；不保证调度算法本身免疫墙钟变化。

它是 **Linux 容器的运行角色适配器**，不是跨平台 Agent 内核要求；缺 BOOTTIME 或
procfs 时探针失败关闭。观测失败不阻止 Beat 原本调度，日志为固定错误码，尝试频率
受单调时钟限制；文件停止更新后 reader 会判过期。未对抗同一可信 UID 的恶意伪造，
也不是浏览器可随意读取本地路径的 API；不要给其他 Pod 挂共享宿主目录来读取它。

## 验收边界与回滚

单测覆盖真实父类调度和 schedule 文件、发送异常、快照原子性、坏输入、进程/启动
标识、墙钟/mtime 无关性、暂停/过期拒绝、观测失败不影响调度及日志限频。
真实独立 Beat/RabbitMQ 测试只使用随机合成队列：验证 broker 中确有消息，SIGSTOP
时拒绝、SIGCONT 后 tick 推进、退出后旧文件拒绝、同 schedule 重启后新 PID 可见。
这些都不是 Worker 的 Inbox/业务副作用完成证据，更不是当前业务部署验收。

本包**没有接 Kubernetes liveness/readiness/startupProbe 或自动重启**。仅凭旧快照
就自动重启，可能在正常长等待或 broker 故障时形成重启循环；必须另有运行策略和故障
验收。实际告警采集、Worker 消费进展仍欠账。
回滚时恢复 bootstrap 的默认 Celery scheduler 选择即可，旧 schedule 格式不变；临时
activity JSON 可保留但读者须停用，不需要数据库降级。业务镜像、部署和数据不在本包变更。
