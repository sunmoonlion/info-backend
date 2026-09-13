# 只读投递观测（B7d）

在后端 app 目录运行：

```bash
python -m app.cli.delivery_metrics
python -m app.cli.delivery_metrics --format prometheus
```

入口只打印 JSON v1 或 Prometheus text 0.0.4，不启动 HTTP 服务、不写采集文件。
使用现有 Settings 的本 App 数据库连接；操作人必须已有对应 DB 权限，不能把命令
或凭据交给浏览器。公共观测只需相关表 SELECT；领域 observer 另需其租约表 SELECT。
不调用 claim、pump、reconcile、replay 或 GC，不联 broker，不依赖 Web 登录。

## 统计语义

`delivery_observers.py` 是显式领域扩展点：默认包装现有 DurableTasks 的 handler 清单；
领域应装配实际 Delivery 实例，不能复制 topic/consumer/lease 的影子配置。
模板没有领域 topic 是正常空清单；未注册消息单独报聚合数量，不将 DB topic 自动注册。
Investment 加入实际 AgentDelivery：使用 agent.executor 回执和 Agent 租约，notification
没有业务 Inbox，发布成功即满足传输完成；绝不把 notification 当必需 Agent 回执。

| gauge 字段 | 含义 |
| --- | --- |
| messages / incomplete_messages | 保留记录数 / 未达到该 policy 完成条件的记录数 |
| scheduled_messages | 未完成且 immutable not-before 尚未到期，不等于退避 available_at |
| publishable_messages | 已到期、无活跃执行/死信、未完成，pending 或发布租约过期且退避已到期 |
| awaiting_receipt_messages | published 但缺必需业务回执，可能仍正常执行或已有死信 |
| active_execution_messages | 未完成且该 policy 执行租约有效的消息数，不是 Worker/进程数 |
| expired_publisher_leases | 未完成 delivering 且发布租约过期，不是执行租约数 |
| dead_letter_messages | 尚未显式 replay 的死信记录数 |
| reconcile_candidates | published、缺必需回执、超过该 policy lease_seconds、无活跃执行/死信 |
| oldest_due_incomplete_age_seconds | 已到期、未完成且非死信的最老年龄，起点取 created_at/not-before 较晚者；包含正常执行及退避中的任务 |
| unregistered_messages | 不在任何 observer topic 中的 Outbox 数量；须调查，不等于可安全删除 |
| snapshot_timestamp_seconds | 采集开始的 DB 时间；用于识别过时文本，不能当心跳证明 |

所有计数均为 gauge；保留窗/回滚/归档会改变数值，不使用 `_total` 假装不可回退 counter。
无消息 ID、用户、dataset、payload、错误正文或动态数据库标签。标签只来自经过界限
校验的代码 policy/topic（最多 16 policies、128 topics）；重叠 topic 配置拒绝。
快照为 REPEATABLE READ / READ ONLY，整体 2 秒协作式预算，单条 SQL 1.5 秒；
租约检查沿用实际 policy 的 DB 时钟，年龄基准为快照开始时间，不声称所有时钟调用同一纳秒。
权限、缺表、非法时间字段、超时失败时退出 2，只打印通用错误，不打印半份或假零指标。
取消向上传播。大账本扫描仍有成本，超时不是性能容量证明，需要按实际规模验收。

## 仍待上线接线

本包不是 Prometheus scrape 服务、textfile collector 定时作业、告警路由或 Pod 健康探针。
后续须确定采集 principal、调度周期、原子写入/过期清除、受保护 scrape 边界、告警阈值和
接收者，再实测断采/凭据撤销/DB 故障/告警到达。可优先观察未注册消息、死信、待对账数量
及最老未完成年龄；阈值必须考虑长任务/计划延迟，不能默认零容忍并误报运行中任务。
不得把旧文本保留当本次采集成功；任何消费者必须检查快照新鲜度和命令退出码。

空积压不证明 Worker 消费队列正确，有效租约不证明业务有进展，旧租约保留行不等于故障。
未涵盖 RabbitMQ queue 指标、Scheduler heartbeat、retrieval/run/SSE 的产品指标；这些
仍由 B7/N4 对应范围接收，未因本次观测输出而完成。归档/删除也尚未获准或实现。

文本按 [Prometheus 官方 exposition format](https://prometheus.io/docs/instrumenting/exposition_formats/)
组织 HELP/TYPE 与唯一标签样本，末尾换行；不引入进程内累加器或第二份持久化业务账。
