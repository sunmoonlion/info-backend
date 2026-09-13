# Info 逻辑交付身份（B7c / 0009）

同一 `document_version_id + target_app + dataset_key` 只有一条 `distribution_record`。
`None`、空串和 `default` 沿用现有创建接口的 default 语义；不折叠其它大小写/空白。
不存在新状态表、第二份幂等台账或跨 App 读库。

创建锁定既有版本行，再锁定并刷新可能已有的交付记录；数据库表达式唯一索引兜底。
重复创建返回原 ID，原 artifact 快照、下游幂等键、历史、错误和回执保持不变。
`dispatch=True` 只为 pending 记录确保原代次的公共 Outbox 意图；不会隐式重试
failed、running、succeeded 等状态。显式 retry 继续复用记录，并生成下一代命令。
HTTP 响应结构和既有 201 不变；201 不代表每次都新增一行。

## 迁移与切换边界

- 线性迁移 `20260913_0009` 继承 `20260912_0008`；需 online 数据核查，拒绝 offline SQL。
- 可先使用 `python -m app.cli.distribution_preflight`，仅通过 Settings 连接本 App 库。
  REPEATABLE READ / READ ONLY，输出总量、冲突数和最多 50 个记录 ID 样本，退出 0/2。
  不输出 dataset 名称、URL、文件内容或 payload，不写库。大库须安排扫描窗口。
- 迁移锁表并重新核查，任何冲突即失败；不选赢家、不合并、不删记录、不改历史请求。
  冲突涉及的 Outbox/Inbox、远端回执与实际效果必须单独调查并获处置批准。
- 无冲突时保留 NULL/空 dataset 原值，建立表达式唯一索引；身份字段不可改写。
  `write_protocol_version=1` 回填只表示新存储写协议，**不证明旧 payload 可执行或已授权**。
  ORM 提供 Python 默认值，数据库无默认值，旧 INSERT 和未知协议失败关闭。
- 复用不会重建旧 payload；旧协议/失效 Artifact/授权等仍受既有消费门禁约束，不能
  以本次去重迁移宣称旧任务已安全转换。停用旧 API/Worker/Scheduler 后一致切换，禁止混跑。
- API readiness 会要求 0009。降级只移除本次约束、触发器和协议列，不删业务行；
  降级后不能再声称逻辑唯一保证。回滚须停写，使用配套旧源码与经过验证的备份恢复方案。

本包只在一次性 PostgreSQL/S3 上验证。业务库冲突核查、备份恢复、最小权限、锁窗口、
迁移角色、旧任务分类与运行角色统一切换仍是上线门禁；没有运行业务迁移或部署。
