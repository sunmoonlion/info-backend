# v5 M1-102 / B2.2：跨 Worker 来源并发

日期：2026-09-12。Luna 单人实施、自测；源码候选，不表示已部署。
原任务及批准范围见 k8s 的 `sunmoonai/docs/v5-backlog-disposition-luna.md`。
起点：本仓 `19243109988f949c6d9520c14f03cb0e2f6fe8fc`（B2.1 网络防护）。

## 范围与固定策略

首版每个逻辑来源最多一个执行；来源 UUID 为主键，没有来源时按目标 URL 的主机归组。
匿名来源忽略路径、查询、片段、scheme、端口；主机统一为小写 IDNA、去末尾点并规范化 IP。
这不是每个物理网站/IP 的总配额：不同来源 ID、DNS 别名或跳转目标可能指向同一站点。
不新增可由调用方调大的并发配置，不声称实现限速、robots/版权治理或公平调度。

`infrastructure/storage/crawl_concurrency.py` 使用 Info 数据库的
`pg_try_advisory_xact_lock`，稳定命名空间 SHA-256 截取有符号 64 位键；哈希碰撞只会
额外串行化，不会放开并发。不是 Python 进程内锁，不依赖 Python hash 随机种子。
使用独立连接和独立事务，因此正文抓取的中间业务提交不会释放来源锁。

- 正文抓取：准入先于 `running`、抓取次数、HTTP 和对象存储写入；覆盖整个抓取处理。
- 采集发现：共享同一来源键，覆盖 adapter discovery；忙时不调用 adapter、不新建任务。
- 相同消息的幂等/租约仍由公共 DurableTasks 管理；来源锁只控制不同作业的资源竞争。
- SourceBusy 逃出领域终态转换，Worker 不写失败终态和 Inbox；不复制或修改公共底座。
- discovery 接口返回 409 / `crawl_source_busy` / `Retry-After: 5`，既有身份/CSRF 边界不改。
- 文件上传没有出网抓取，不增加来源锁；外部 Scrapy/Playwright 进程不受此本地锁控制。

无数据库迁移、无依赖或公共模板变更；本包是 Info 领域并发策略，不是新的通用调度器。

## 重排不是无限等待

消费忙退出后，原 Outbox 保留、Inbox 缺席；公共 reconcile 超过回执等待窗后重排同一个
消息，重新发布后再尝试来源锁。不新建消息链，不在 Worker 里 sleep 等锁。
现有默认执行/回执租期 60 秒、发布次数上限 10；不是按 Retry-After 的 5 秒重试 Worker。
持续争用会耗尽公共上限并进入 `consumer_unacknowledged` 死信，业务抓取仍保持非终态；
管理员应检查竞争/容量后按既有 CLI 显式 replay。不能把死信当业务成功，也不承诺零饥饿。
将来若需要不计失败额度的公平资源等待，应先在模板设计统一延迟/预算机制，再同步实例。

## 测试与边界

新增测试随常规 pytest 收集：

- `test_crawl_concurrency.py`：主机别名/IPv6/IDNA、稳定来源键、无 DB 失败关闭、409 映射。
- `test_crawl_concurrency_db.py`：真实 PostgreSQL 独立连接同源互斥、异源并行、业务提交
  不释放、异常/取消释放、真实子进程 `os._exit` 后重获锁。
- 同一条真实 Outbox：忙时零抓取/零制品/零 Inbox/不增加业务次数，reconcile 原消息，
  验证重试、耗尽死信、显式 replay、最终成功和重复消息不再抓取。
- 发现与抓取同时竞争；实际运行中的抓取已提交 running 后仍排斥同源、允许异源，
  取消运行中 Worker 后可重新消费。
- 保留 B2.1 抓取边界测试；原静态检查更新到明确的准入后函数，同时检查入口确有准入调用。

测试只接一次性 `info_backlog_tests`、随机 schema；HTTP 与 S3 用注入替身，
这不是公网负向测试、真实对象存储或 Kubernetes 运行验收。具体全套结果记录在处置清单。
本轮专项及原 Info 投递回归 30 passed（5.26 秒）；全套携带模板共享契约向量
**203 passed / 0 skipped**（9.37 秒），Ruff/Pyright 通过。

部署前注意：每个获准执行额外占一个数据库连接/事务，连接池耗尽仍可能等待其 pool timeout，
“非阻塞”只指不等待别人持有的 advisory lock。API/Worker 必须使用同一 Info 逻辑数据库，
且全部更新才受相同策略约束；数据库用户须能调用该 PostgreSQL 函数。
不能把此锁冒充外部系统 fencing：极端分区/数据库重启时，已经发到外部的 HTTP 请求无法
凭数据库锁强制撤回。未验证这些故障下的硬并发上界，也不宣称实现全产品 Task 调度。

原语依据：[PostgreSQL advisory locks](https://www.postgresql.org/docs/current/explicit-locking.html#ADVISORY-LOCKS)。

## 回滚

撤销本包的反向源码提交并复跑测试；无表结构/业务数据回滚。不自动部署或重开死信。
移除锁会恢复无限制来源并发，不能将其作为未经容量评估的生产恢复默认操作。
