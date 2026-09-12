# v5 M1-103 / B4：Artifact 可追踪与只读对账

2026-09-12；Luna 单人实施、自测。起点 `info-backend@14d57d8`。
本包是源码候选，未扫描业务存储、未部署、未启用周期任务，未清理任何业务对象。
总处置清单：k8s `sunmoonai/docs/v5-backlog-disposition-luna.md`。

## 1. 旧账处置与边界

旧 M1-103 要求不可变 object key/version/hash、staging/committed、最小孤儿对账，
其业务判据是“数据库失败不会留下永久不可追踪对象”。现有 RawArtifact 是主档；
对象在 DB 提交前就可能成功写入，甚至 PUT 已被服务端接受但客户端未取得回执。

本包沿用现有 S3 版本引用，不新建第二份制品表或分布式事务。**最小对账是只读检测，
不是自动回收器**：从 S3 的全部版本反查 RawArtifact，再从 RawArtifact 按精确版本核存储。
即使 job/RawArtifact 事务完全回滚，配置的 Info 前缀仍能列出遗留版本及其可重取定位。

旧 staging/committed 意图按已有事实实现：DB 登记是否提交由 RawArtifact 的可见性决定，
不持久化一份可能再次漂移的 staging 标志。未登记观测不等于已经证明事务失败；也不等于
未来可以删除。`unregistered_recent` / `unregistered_candidate` 只是这次扫描的分类，
不改变领域 `storage_state`，不新增 Task/Attempt 状态，不自动补写失去的文档或投递记录。

这关闭的是**可发现、可定位、可重取及只读对账**子项；自动修复/回收、保留策略、
周期调度与告警进入 B7/N4，仍未实现。不能说整个对象生命周期自动化已经完成。

## 2. 两个方向与报告含义

固定范围：现有配置的 `S3_BUCKET` 内 `info/original/`，不能通过 cursor 改 bucket/前缀。
只操作 Info 逻辑库，不跨域查 Knowledge，也不扫描宿主机目录。

| 报告分类 | 含义及处置边界 |
| --- | --- |
| registered_metadata_match | 精确 VersionId 的长度、SHA-256 metadata 与 RawArtifact 相同；不是全文重新哈希验收 |
| unregistered_recent | 查不到登记、对象未满 24 小时，可能是写入中；重新扫描，不标“可删” |
| unregistered_candidate | 满 24 小时仍查不到本包核查的登记；可能丢回执/回滚/其他残留，仅供受控调查 |
| protected_ambiguous_reference | 旧无 VersionId 的 RawArtifact 或 ExtractedContent 同 key 引用；不能确定版本，保守保护 |
| missing_version | HEAD 精确版本返回 404/NoSuchVersion 等；不回写 available、不清理引用 |
| storage_unavailable | 403、5xx、网络异常等，不能解释为不存在 |
| metadata_mismatch | 长度、版本或 SHA metadata 不一致；阻断核验结论，保留两端证据 |
| unversioned_object / unversioned_reference | 不可变版本条件不满足，需要独立治理，不默认修复 |
| delete_marker | 存储中的删除标记，不等于历史版本已经消失，也不删除它 |

所有 RawArtifact 均算登记，**不要求 document_version_id 非空**：未变内容重抓、失败抽取
留下的原文/响应头是有效诊断材料。对账并不追溯所有历史分发快照、外部备份/法律保留引用；
这也是 `deletion_authorized` 永远为 false 的原因之一。

每次最多 100 项（默认 50），一次一个页面。inventory 按 S3 的 KeyMarker 和 VersionIdMarker
共同续扫，包括非当前版本和删除标记；references 按 RawArtifact UUID 主键续扫。
`end_of_listing=true` 只表示该方向从给定起点扫到末页，不是全库/全 bucket/全生命周期健康。
要核全量，须分别从两个方向的空 cursor 开始，保留全部页面及问题，不能只看最后一页退出码。

每页 DB 使用 REPEATABLE READ / READ ONLY；S3 和 DB 没有共同快照，多页也不是同一个快照。
新写入可能落在游标前，所以走到末页后还要定期从头复扫，尤其复核上次未登记对象。
24 小时是提示分类门槛，**不是租约时长、保留期或删除安全窗**。

## 3. 写后核验与兼容性

原实现 PUT 后 HEAD latest；并发覆盖可能让 HEAD 读到另一个版本。本包改为严格使用
PUT 回执的 VersionId，检查返回版本、长度及 SHA metadata。客户端有连接 3 秒、读取 5 秒、
最多 2 次总尝试的显式边界；客户端关闭释放连接。SDK 重试可能生成多个版本，inventory
会保留并发现没有登记的额外版本，不承诺 S3 PUT exactly-once。

S3 写入返回无版本或 `null` 现在失败关闭，不能继续登记为可用制品。由于 PUT 已发生，
对象可能已经存在；inventory 报 unversioned_object。部署前必须确认 bucket Versioning
已 Enabled，不能在业务写入上尝试猜测是否开启。未新增配置、依赖或数据库 migration。
现有 0008 源码迁移仍须按 B3 的独立切换条件执行，不因 B4 无迁移就绕过它。

本地文件 storage 分支保持原状（开发用途，没有不可变 VersionId，不能作为跨域分发制品）；
本 CLI 对 STORAGE_BACKEND=local 明确拒绝，不把云端目录或客户目录偷偷当 S3 库存。

## 4. 运维入口与权限

只读运维身份经现有配置注入：Info DB 的 SELECT 权限；inventory 需要配置 bucket 的
`s3:ListBucketVersions`，两方向核验需要前缀内 `s3:GetObjectVersion`。
**不需要 DeleteObject、DeleteObjectVersion、PutObject 或修改 bucket Versioning 权限**。
运行代码使用 SDK HEAD/LIST，不读正文；故障演练里的 GET/DELETE 仅属于随机测试 bucket。
实际最小权限角色矩阵仍待受权环境验收，本包未改 Secret/凭据或策略。

```bash
cd app
uv run python -m app.cli.reconcile_artifacts --mode inventory --limit 50
uv run python -m app.cli.reconcile_artifacts --mode references --limit 50
# 下一页：把上一页 next_cursor 的完整 JSON 作为 --cursor 参数；不要只保留 key marker。
# 私有运维终端需要定位时可加 --include-object-keys。
```

items 默认只输出对象指纹，references 另含 artifact_id；显式参数才输出对象定位。
**整个报告仍按私有运维材料处理**：续扫 cursor 包含真实 S3 key marker，可能带文件名；
不是匿名化报告，不发浏览器、不提交进 Git。异常只输出类型，不打印凭据、SQL 或对象 key。

退出码：0=本页无问题且到末页；1=页面未完成/配置或读取异常；2=有需要复核的分类；
3=本页无问题但还有后页。2 也可能带 next_cursor，必须保留问题并继续受控扫描。
CLI 每页协作超时 60 秒、DB 单条语句 5 秒；超时不输出“已完成”或续扫成功。
线程中的 SDK 请求可能到 socket 超时后才退出，不将 60 秒宣称为硬进程死亡上界。
大库扫描可能受查询耗时限额影响；索引/长期调度按真实规模另验，不自动扩权或无限重试。

## 5. 故障验证与交付

- 一次性 PostgreSQL 17.6 + pgsty/minio RELEASE.2026-03-25，独立端口、无业务卷；测试
  创建 `luna-b4-tests-<uuid>` bucket，启用版本，每测后只移除该测试 bucket/对象。
- 全套含共享契约向量：**278 passed / 0 skipped**（16.94 秒）；Ruff/Pyright 通过。
- 真实 S3：正常上传三制品双向一致；DB/索引意图失败后 DB 全回滚，但三个对象可列举、
  按精确版本 GET 重取；重试后原残留与新登记分开；成功 PUT 丢回执后仍可发现对象。
- 真实在途事务先报 recent，提交后复扫转为 registered；同 key 的历史版本与删除标记
  逐页续扫不丢；并发覆盖仍核本次版本；显式删除测试版本后报告 missing，DB 记录不变；
  测试 bucket 暂停 Versioning 后写入失败关闭，遗留 null 版本仍被发现。
- 注入验证 403/404/5xx/网络错误、长度 metadata 不一致、旧无版本引用、ExtractedContent
  模糊引用、页面上限/越域游标/不前进游标、CLI 退出码及异常脱敏。
- 没有业务 S3/DB 实测、真实生产 IAM 拒绝矩阵、对象正文全量校验、KIND 部署、镜像变更。

回滚使用代码反向提交，无数据降级；不删除扫描候选或回填主档。回退代码会恢复原来的
latest 核验风险，应停写评估。业务中的修复或回收必须另有对象精确版本清单、全引用核验、
备份/保留审批和恢复计划，本命令任何报告都不构成授权。

SDK 原语依据：[S3 版本枚举](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/list_object_versions.html)、
[HEAD 精确版本](https://docs.aws.amazon.com/boto3/latest/reference/services/s3/client/head_object.html)。
