# 按证券代码采集

设计见 k8s 仓 `sunmoonai/docs/dev-investment-agent/tree-build/SDD/modules/0008-info.md`「段一」。
本文只写怎么用、放在哪、已知的事实。

## 做什么

给一个六位 A 股代码，采两样东西，原始响应原样留存：

| 来源 | 登记的版权状态 | 内容 |
| --- | --- | --- |
| `cninfo` 巨潮资讯网，法定披露渠道 | `public_disclosure` | 证券列表、年报公告查询、每份年度报告的 PDF（近八年，摘要与英文版不要，修订版保留并标记） |
| `eastmoney-f10` 东方财富 F10，第三方网站 | `unconfirmed_internal_only` | 公司类型页、三大报表的报告期列表、按报告期的报表数据（每五期一个请求） |

银行、保险、券商（公司类型不是一般工商业）和北交所证券目前拒绝，错误码 `unsupported_company_type`、`unsupported_market`。

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
| `Q-R01` 至 `Q-R09` | 九条同期勾稽，容差一元 | 拦住 |
| `Q-CONT` | 本年期初现金等于上年期末现金；不连续必须能由追溯调整解释 | 拦住 |
| `Q-OFFICIAL` | 标了口径的年份，关键科目与年报原文逐项一致 | 拦住 |
| `Q-BASIS` | 有年报原文的年份，报表必须与原文的某个口径一致 | 拦住 |
| `Q-DISCLOSURE` | 每个年度都有法定披露日 | 提醒 |
| `Q-FRESH` | 最新一期距今不超过 200 天 | 提醒 |

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

## 已知事实（2026-09-27 实测，600009）

- 九份年报（2017 至 2025 年）全部解析成功，共 177 个关键数字。关键页在第 6 或第 7 页。
- 判定结果：2017 至 2020 年、2022 至 2025 年为原始披露，2021 年为追溯调整后。与当天手工核对的结果一致。
- 十五项检查全部通过。跨期检查发现 2021 年期初现金与 2020 年期末现金差 5353.86 万元，由 2021 年的追溯调整解释。
- 建一次约 30 秒，内存峰值约 220 MB，主要花在 PDF 抽取上。数据集文件约 140 KB。

## 测试

- `tests/test_security_dataset.py`：解析、口径判定、质量检查、建库。夹具是实采数据裁出来的，数值没有改。
- `tests/test_security_dataset_db.py`：读回原文、留存与登记，需要 `DELIVERY_TEST_DATABASE_URL`。
