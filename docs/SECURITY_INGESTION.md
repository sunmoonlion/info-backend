# 按证券代码采集

设计见 k8s 仓 `sunmoonai/docs/dev-investment-agent/tree-build/SDD/modules/0008-info.md`「段一」。
本文只写怎么用、放在哪、已知的事实。

## 做什么

给一个六位 A 股代码，采两样东西，原始响应原样留存：

| 来源 | 登记的版权状态 | 内容 |
| --- | --- | --- |
| `cninfo` 巨潮资讯网，法定披露渠道 | `public_disclosure` | 证券列表、年报公告查询、每份年度报告的 PDF（近八年，摘要与英文版不要，修订版保留并标记） |
| `eastmoney-f10` 东方财富 F10，第三方网站 | `unconfirmed_internal_only` | 公司类型页、三大报表的报告期列表、按报告期的报表数据（每五期一个请求） |

银行、保险、券商（公司类型不是一般工商业）目前拒绝，错误码 `unsupported_company_type`。
北交所证券 2026-09-28 起支持（第三方网站用前缀 `BJ`，实测 `BJ920185` 可取）。

接口地址与参数参考了 akshare 1.18.97（MIT License）的四个函数，出处写在各采集器的文件头。运行时不依赖 akshare。

## 怎么用

命令行，登记并立即执行：

```bash
cd info-backend/app
uv run python -m app.cli.security_ingest --code 600009
```

退出码 0 成功，2 批次失败（原因在输出的 `error_code`），1 参数或运行环境有问题。

管理接口，登记后由后台任务执行，立即返回：

```text
POST /api/admin/securities/{code}/ingestions        → 202，返回批次
GET  /api/admin/securities/{code}/ingestions        → 该代码的批次列表
GET  /api/admin/security-ingestions/{ingestion_id}  → 批次与每个条目
```

后台任务的主题是 `info.security.ingest.v1`。批次失败是批次的终态，不触发重新投递；同一批次被重复投递时不会再跑，上次执行被打断的批次记为 `interrupted`。

## 放在哪

| 层 | 位置 | 内容 |
| --- | --- | --- |
| 领域 | `app/domain/securities/` | 证券代码、请求、留存凭据、批次摘要、错误 |
| 端口 | `app/application/ports/securities.py` | 取数、留存与登记两个协议 |
| 应用 | `app/application/securities/` | 两个采集器、采集服务。只依赖领域与端口 |
| 基础设施 | `app/infrastructure/securities/` | 取数适配、PostgreSQL 与对象存储的实现 |
| 组装 | `app/bootstrap/securities.py` | 把实现接到服务上 |
| 入口 | `app/cli/security_ingest.py`、`app/interfaces/http/admin/securities.py` | 命令行、管理接口 |

数据表：`security_ingestion`（批次）、`security_ingestion_item`（条目），迁移 `20260927_0010`。每个请求另有一条 `crawl_job`（请求记录）；响应原文是一条 `raw_artifact`。

对象键由内容决定：`info/securities/code=<代码>/source=<来源>/sha256=<前两位>/<sha256>/<文件名>`。同一内容重采不新增对象，条目的 `reused` 为真。

## 取数边界的两处扩展

都在 `app/infrastructure/external/crawl_http.py`，默认行为不变：

- `method="POST"` 与 `form`：只用于公开查询接口的检索条件，表单体不超过 16 KB；POST 不跟随重定向。
- `decode=True`：服务器无视「不要压缩」的请求头而返回 gzip 或 deflate 时，边解压边计数，压缩输入与解压输出都不超过 `max_bytes`，超限即止。其它编码仍拒绝。

## 大小与时限（2026-09-28）

| 设置 | 默认 | 含义 |
| --- | --- | --- |
| `CRAWL_MAX_BYTES` | 不变 | 一般请求的响应上限 |
| `SECURITY_REPORT_MAX_BYTES` | 128 MiB | 单份年报 PDF 的上限。此前写死 40 MB，而一般上限又只有 10 MB，大公司的年报取不下来 |
| `SECURITY_REPORT_TIMEOUT_SECONDS` | 180 | 单份年报的下载时限 |

某个请求可以要求比默认更大的上限，但不能超过取数适配的封顶值；封顶值由上面两项设置给出，不低于默认值。

建库时读回原件的上限跟着 `SECURITY_REPORT_MAX_BYTES` 走（不低于 64 MiB）。此前读回的上限写死 64 MiB：
紫金矿业 2025 年年报 79.9 MB，采得到、读不回，建库时程序直接崩溃（2026-09-28 实测后修正）。

## 年报公告的三处筛选（2026-09-28，紫金矿业实测）

| 情况 | 例子 | 做法 |
| --- | --- | --- |
| 标题写错 | 「紫金矿业集团股份有限公司2024年年报报告」 | 「年报报告」也认作年度报告全文。此前这一年被漏掉，而且不在跳过清单里 |
| H 股年报挂在同一类下 | 「紫金矿业H股市场公告-2021年年度报告」 | 标题含「H股」的不要 |
| 同一份年报重新挂出来 | 标题、披露时间、大小都相同，只有公告编号不同 | 只取编号最小的那条；别的编号记在条目的 `meta.relisted_as` 里 |

## 单份年报失败不再让整批失败（2026-09-28）

年报下载遇到 `fetch_failed`、`http_status` 这两类错误时，跳过这一份，批次继续；
跳过的条目写在批次摘要的 `skipped` 里（来源、类别、名称、错误码、年度）。
一份都没下到才算批次失败，错误码 `no_annual_report_downloaded`。
地址不合法、响应过大、版权状态不符这些仍然让整批失败：它们说明配置或来源有问题，不是偶发。

## 重试

偶发错误最多试三次，间隔 2 秒、4 秒：取数超时、传输失败、解析域名失败、压缩内容损坏，以及 HTTP 429、500、502、503、504。
地址不合法、响应过大、4xx 不重试。只留存最后一次的响应，试了几次记在条目的 `meta.attempts`。

## 已知事实（2026-09-27 实测，600009）

- 一次采集 80 个请求：巨潮 11 个（含 9 份年报 PDF），东方财富 69 个。原文合计约 28.7 MB。耗时 1 到 3 分钟。
- 三大报表分别有 110、110、103 个报告期，最早到 1994 年。
- 东方财富偶尔返回压缩响应，偶尔单个请求超过 20 秒。
- 公告查询的结果里直接带了 PDF 的归档路径 `adjunctUrl`；只接受 `finalpage/<日期>/<编号>.PDF` 这一种形状。
- 公告时间是毫秒时间戳，按上海时间取日期作为法定披露日。

## 测试

- `tests/test_security_ingestion.py`：领域、采集器、采集服务、重试。不访问外网，不需要数据库。夹具是录下的真实响应，只裁了行数。
- `tests/test_security_ingestion_db.py`：留存与登记，需要 `DELIVERY_TEST_DATABASE_URL`。会话配置与生产一致（提交后对象过期）。
- `tests/test_crawl_http.py`：取数边界，含 POST 与有上限的解压。
- `scripts/security_ingest_smoke.sh`：联网冒烟，手动跑，结果在 `scripts/results/`。

# 从采集批次建数据集

设计见同一份文档的「段二」。全部是确定性的计算，不调用模型。

## 怎么用

```bash
cd info-backend/app
uv run python -m app.cli.security_dataset --code 600009            # 用最近一个成功的批次
uv run python -m app.cli.security_dataset --ingestion <批次标识> --out ./sh600009.sqlite
```

退出码 0 已发布，2 质量检查没过（数据集登记为 `quality_failed`，不发布），1 原文或运行环境有问题。

## 做了什么

1. 读回批次里的原文，逐个核对校验值。
2. 把三大报表的原始响应解析成报表行，只取财务目录里登记的字段。
3. 从每份年度报告的「主要会计数据」表里取七个关键科目。表格由 pdfplumber 抽取，只处理前 25 页里出现这个表的页。版式认不出来就记下原因，不猜。
4. 口径判定：报表里某年的数与该年年报「本期」列一致是原始披露；与后面年报里标了「调整后」的列一致，或与后面年报的比较数一致而与当年披露不一致，是追溯调整后；都对不上是未核实。中报季报一律未核实。
5. 质量检查，十五项。
6. 写成 SQLite 文件，连同清单 `manifest.json`、质量报告 `quality.json` 存进对象存储，登记进 `security_dataset` 表。

## 质量检查

| 检查 | 内容 | 不通过时 |
| --- | --- | --- |
| `Q-STRUCT` | 三张表都有数据，报告期不重复，必有字段不为空 | 拦住 |
| `Q-R01` 至 `Q-R09` | 九条同期勾稽，容差一元 | 范围之内拦住；范围之前的报告期先被剔除，不会走到这里 |
| `Q-CONT` | 本年期初现金等于上年期末现金；不连续必须能由追溯调整解释 | 范围之内拦住；范围之前只标注 |
| `Q-OFFICIAL` | 标了口径的年份，关键科目与年报原文逐项一致 | 拦住 |
| `Q-BASIS` | 有年报原文的年份，报表必须与原文的某个口径一致 | 拦住 |
| `Q-DISCLOSURE` | 每个年度都有法定披露日 | 提醒 |
| `Q-FRESH` | 最新一期距今不超过 200 天 | 提醒 |
| `Q-OLD` | 范围之前被剔除的报告期、不连续的年度。没有这类情况时不出现 | 提醒 |

### 只对近若干年硬性拦截（2026-09-28，`F-INFO-13`）

所有者定：第三方数据的质量检查，只对近若干年硬性拦截。几年尚未定，设置
`SECURITY_QUALITY_HARD_YEARS` 默认 10（这是远程的建议值，等所有者定）。

| 项 | 做法 |
| --- | --- |
| 范围怎么算 | 从数据里最新一期所在的年度往回数。最新是 2026 年、取 10，范围就是 2017 年及以后 |
| 范围之内 | 和以前一样，一行不动，任何一条不通过就拦住整个数据集 |
| 范围之前、同期勾稽不平 | 这一期从三张表里一起剔除；写进数据集说明 `excluded_periods_note` 和检查项 `Q-OLD` |
| 范围之前、现金不连续 | 不剔除（同期是平的，只是接不上）；写进 `old_continuity_note` 和 `Q-OLD` |
| 没有这两类情况 | 文件与规则改动之前逐字节相同 |

范围从数据本身算，不从当天日期算：同样的原文永远得到同样的文件。

### 用存档批次重建的结果（2026-09-28，不访问网站）

十二家有成功批次的公司，结果见 `scripts/results/security-dataset-rebuild.20260928-*.txt`。

| 结果 | 公司 |
| --- | --- |
| 发布，文件与改动前逐字节相同 | 600009 |
| 发布（改动前被拦） | 600276、600900、601888 |
| 仍被拦，只因年报的关键数字一个没取到 | 000858、000333、002594、000002 |
| 仍被拦，范围之内确有对不上的地方 | 600519、688981、300750、002415 |

此前采集失败的两家用默认设置重新采集（访问了网站），结果见 `scripts/results/security-recollect.20260928-*.txt`：

| 公司 | 采集 | 数据集 |
| --- | --- | --- |
| 601899 紫金矿业 | 成功。十份年报（2017 至 2025 年，2020 年另有更新版），最大一份 79.9 MB | 发布。只有 2017、2018 两年的年报取到了关键数字，其余年份是未核实 |
| 920185 贝特瑞（北交所） | 成功。五份年报 | 被拦：关键数字一个没取到；2021 年一季报勾稽不平；2019 年现金不连续 |

所以十四家里现在能发布的是五家。

仍被拦的三类原因，都**不是**这条规则能解决的，这次没有动：

| # | 原因 | 涉及 | 说明 |
| --- | --- | --- | --- |
| 1 | 关键数字的解析只认一种版式（表格第一格是「主要会计数据」） | 深交所六家、北交所一家；紫金矿业 2019 年以后 | 每份年报都是 `table_not_found`，`Q-BASIS` 检查数为零，按「一份年报都没有就不发布」拦住。年报抽取做成正式功能之后由它取代 |
| 2 | 容差一元对按千元、万元披露的公司太严 | 300750 | 差额恰好是 100 元、1000 元的整数倍，是四舍五入。容差该不该随披露单位放宽，要所有者定 |
| 3 | 近年的第三方数据确实对不上 | 688981（2017 至 2019 年的中报季报）、002415（2023 年现金不连续）、600519（2021 年与年报两个口径都不一致） | 原因没有逐个查 |

## 数据集的内容

| 表 | 内容 |
| --- | --- |
| `balance_sheet`、`income_statement`、`cash_flow` | 报表行，带 `basis`、`verified`、`verified_against` |
| `official_key_figures` | 年报里的关键数字，带口径、列标签、报告名、披露日、页码 |
| `disclosure_calendar` | 法定披露日历，带原文的校验值 |
| `field_dictionary`、`metric_dictionary`、`reconciliation_rules` | 字段字典、口径表、勾稽规则，来自 `app/domain/securities/financial_catalog.py` |
| `dataset_metadata` | 版本、期间、来源、重述说明、年报对追溯调整的原文说明、会计准则说明、使用权说明 |

数据版本由内容决定：同样的原文和同样的财务目录，得到同样的版本和同样的文件。文件里不写采集时间和批次标识，这两样在清单和登记表里。

财务目录是我们自建的数据。改它等于改数据集的含义。

### 数据集自述第二版（导出版本 2.0.0，2026-09-27）

给知识服务的语义层用（k8s 库 `0009-semantic`）。原有的表与列都没有动，旧的读法不受影响。

| 位置 | 内容 |
| --- | --- |
| `metric_dictionary` 新增六列 | `kind`（现在只有 `row`：基础表的一行算出一个值，不做汇总）、`base_table`、`value_expression`、`applicable_when`、`reason_if_not`、`queryable` |
| 新表 `table_links` | 表间关系：三张报表两两之间，同一家公司同一个报告期的行互相对得上 |
| 新表 `table_keys` | 一张表的一行由哪些字段确定，再带哪些字段才看得懂这一行 |

约定：

- 表达式只引用基础表的字段；引用有关系的表写成 `表名.字段`。
- 以金额为分母的口径，分母为负或在一元以内时不适用；`applicable_when` 为空表示总是适用。
- 一行表达不了的口径（平均净资产收益率要用上一期的数）`queryable` 为 0，只有说明。
- 比率类口径的单位由 `%` 改为 `比率`：值是 0.25 这样的数，不是 25。

用上次实采留存的批次重建（不访问网站）：数据版本 `sh600009-financials-9fd91db79529e208`，
文件 sha256 `ac5cfd08…bac23`，151552 字节，质量检查全部通过，两次重建的文件相同。
上一版是 `sh600009-financials-39a395bfa6f16b67`。结果见 `scripts/results/security-dataset-rebuild.*.txt`。

## 已知事实（2026-09-27 实测，600009）

- 九份年报（2017 至 2025 年）全部解析成功，共 177 个关键数字。关键页在第 6 或第 7 页。
- 判定结果：2017 至 2020 年、2022 至 2025 年为原始披露，2021 年为追溯调整后。与当天手工核对的结果一致。
- 十五项检查全部通过。跨期检查发现 2021 年期初现金与 2020 年期末现金差 5353.86 万元，由 2021 年的追溯调整解释。
- 建一次约 30 秒，内存峰值约 220 MB，主要花在 PDF 抽取上。数据集文件约 140 KB。

## 测试

- `tests/test_security_dataset.py`：解析、口径判定、质量检查、建库。夹具是实采数据裁出来的，数值没有改。
- `tests/test_security_dataset_db.py`：读回原文、留存与登记，需要 `DELIVERY_TEST_DATABASE_URL`。

# 向知识服务登记，以及三步任务链

> 设计：k8s 库 `tree-build/SDD/modules/0008-info.md`「数据集文件进对象存储，向知识服务登记」。2026-09-27。
> 对方的接口说明在 knowledge-backend 的 `docs/dataset-registry.md`。

## 做什么

数据集建好并通过质量检查之后，把它的**位置与校验值**登记到知识服务。文件不经 info 传过去：
知识服务按登记的桶、对象键、对象版本、sha256 自己去对象存储取，取到的内容对不上就不用。

## 三步任务链

管理接口发起采集后，三步各是一个持久任务，前一步成功才排下一步；下一步和本步的完成记录在同一个事务里提交，
所以不会漏排，重复投递也不会重复排。

| 任务 | 做什么 | 什么时候排下一步 | 失败怎么办 |
| --- | --- | --- | --- |
| `info.security.ingest.v1` | 采集一个批次 | 批次成功 | 采集失败是批次的终态，不重投 |
| `info.security.dataset.build.v1` | 从批次建数据集 | 质量检查通过，且知识服务已配置 | 原文有问题（缺表等）记日志后结束；存储、数据库故障由投递机制重试 |
| `info.security.dataset.register.v1` | 向知识服务登记 | 无 | 对方不可达、限流、5xx：重试；被拒绝（冲突、不合规、身份不对、对方没开登记）：记下错误码，不重试 |

质量检查没过的数据集不登记（`F-INFO-07`）；任务这一层和登记服务自身各拦一次。

## 配置

| 环境变量 | 含义 |
| --- | --- |
| `KNOWLEDGE_APP_DATASET_URL` | 知识服务登记接口的完整地址，`…/api/internal/v1/knowledge/datasets` |
| `KNOWLEDGE_APP_SERVICE_CLIENT_ID`、`KNOWLEDGE_APP_SERVICE_CLIENT_SECRET` | 与文档入库用同一个服务身份，权限 `knowledge:ingest` |

三项不全时视为没配置：建好数据集后不排登记任务，手动登记返回 `registrar_not_configured`。

部署时还要两件事（段五）：知识服务打开 `knowledge_dataset_registry_enabled`，把 info 的桶写进
`knowledge_dataset_allowed_buckets`；知识服务的存储账号对 info 的桶有只读权限。

## 怎么用

```
# 建库并登记（命令行；退出码 3 表示已发布但登记没成功）
python -m app.cli.security_dataset --code 600009 --register

# 看某代码建出过的数据集、质量检查结果、登记结果
GET  /api/admin/securities/600009/datasets

# 手动登记某个版本：登记失败后的重试，或回退到旧版本
POST /api/admin/security-datasets/{数据集记录标识}/registration
```

登记的结果记在 `security_dataset` 上：`knowledge_registered_at`（最近一次成功的时间）、
`knowledge_registration_error`（最近一次失败的错误码，成功后清空）。

| 错误码 | 含义 | 重试 |
| --- | --- | --- |
| `knowledge_unreachable` | 连不上 | 是 |
| `service_token_unavailable` | 换不到服务令牌 | 是 |
| `knowledge_request_failed` | 对方返回未分类的状态码（细节里有状态码） | 429、5xx 是 |
| `knowledge_rejected_identity` | 401、403 | 否 |
| `knowledge_registry_disabled` | 对方没开多数据集 | 否 |
| `knowledge_version_conflict` | 同一版本已登记了不同的内容 | 否 |
| `knowledge_refused_registration` | 对方认为登记不合规（例如桶不在允许清单） | 否 |
| `knowledge_reply_unexpected` | 对方答复的不是我们登记的版本，或不是现行版本 | 否 |
| `dataset_not_published`、`dataset_not_found`、`no_published_dataset`、`registrar_not_configured` | 本地就拦下的 | 否 |

对方的响应正文、地址、令牌都不进错误信息与日志。

## 还没在真实环境验过的

本机没有对象存储服务，所以「info 登记 → 知识服务从对象存储取回文件」这一段只在两边各自用替身验过：
info 发出的请求体与知识服务的接口定义逐项对应；知识服务用 info 实建的 600009 数据集原件验证读得了。
两个服务连起来跑要等段五。

## 测试

| 文件 | 验什么 |
| --- | --- |
| `tests/test_security_registration.py` | 请求体、失败的分类、答复的核对、不泄露细节、应用服务的规则 |
| `tests/test_security_registration_db.py` | 登记记录；三步任务链在真的持久任务运行时上的衔接、重试与不重试 |
| `tests/test_security_routes_db.py` | 管理接口（含段一的采集接口） |
