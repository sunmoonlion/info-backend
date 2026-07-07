# Info Admin Backend Spider MVP

本目录实现 `info-app` 第一阶段采集闭环：

```text
URL -> crawl_job -> raw artifact -> extracted content -> info_document -> document_version
```

## 1. 数据库迁移

在 `info-admin-backend/app` 下执行：

```bash
uv sync
uv run alembic upgrade head
```

迁移不依赖 `uuid-ossp` 等需要超管权限的扩展；UUID 主键由应用侧生成。

## 2. 本地对象存储

默认 `.env.example` 使用本地对象存储 fallback：

```text
STORAGE_BACKEND=local
STORAGE_LOCAL_ROOT=.local-storage/info-originals
S3_BUCKET=development-info-originals
```

产物会写入：

```text
.local-storage/info-originals/development-info-originals/
```

平台 S3 已用本机 kind MinIO/AIStor 验证：`STORAGE_BACKEND=s3` 下 crawl job
可写入 `raw.html`、`headers.json`、`clean.md` 和 `text.txt`，并记录对象
`version_id`。

K8S 环境应设置：

```text
STORAGE_BACKEND=s3
S3_ENDPOINT=...
S3_REGION=...
S3_ACCESS_KEY_ID=...
S3_SECRET_ACCESS_KEY=...
S3_BUCKET=...
S3_FORCE_PATH_STYLE=true
S3_USE_TLS=false
```

## 3. 搜索索引

Elasticsearch/OpenSearch 是可重建读模型，默认关闭：

```text
SEARCH_BACKEND=disabled
ELASTICSEARCH_URL=https://localhost:9200
ELASTICSEARCH_INDEX=info-information
ELASTICSEARCH_TIMEOUT_SECONDS=10
```

启用时设置：

```text
SEARCH_BACKEND=elasticsearch
```

重建索引会从 PostgreSQL 读取 `document_version`，并携带 S3 artifact 引用：

```bash
curl -X POST 'http://localhost:8000/api/admin/search-index/rebuild?limit=200'
```

当前重建入口会自动创建 `info-information` 索引。创建新的 `document_version`
并提交成功后，会优先投递 Celery 后台任务写入索引；未配置 Celery broker
时会在主事务提交后执行一次 best-effort 增量写入，搜索服务故障不会回滚采集结果。

## 4. API

创建来源：

```bash
curl -X POST http://localhost:8000/api/admin/sources \
  -H 'Content-Type: application/json' \
  -d '{"code":"manual","name":"Manual","source_type":"website"}'
```

创建采集任务：

```bash
curl -X POST http://localhost:8000/api/admin/crawl-jobs \
  -H 'Content-Type: application/json' \
  -d '{"target_url":"https://example.com","enqueue":false}'
```

创建 RSS collector 并发现 URL：

```bash
curl -X POST http://localhost:8000/api/admin/collectors \
  -H 'Content-Type: application/json' \
  -d '{"code":"example-rss","name":"Example RSS","collector_type":"rss","config":{"feed_url":"https://example.com/rss.xml"}}'

curl -X POST http://localhost:8000/api/admin/collectors/{collector_id}/discover \
  -H 'Content-Type: application/json' \
  -d '{}'
```

创建官方 API collector：

```bash
curl -X POST http://localhost:8000/api/admin/collectors \
  -H 'Content-Type: application/json' \
  -d '{"code":"official-api","name":"Official API","collector_type":"api","config":{"url":"https://example.com/api/news","items_path":"data.items","url_field":"url","title_field":"title"}}'
```

创建 changedetection 触发器：

```bash
curl -X POST http://localhost:8000/api/admin/collectors \
  -H 'Content-Type: application/json' \
  -d '{"code":"watch-home","name":"Watch Home","collector_type":"changedetection","config":{"url":"https://example.com","watch_id":"watch-1"}}'
```

导入专用 crawler worker 产出的 Scrapy / Playwright 发现结果：

```bash
curl -X POST http://localhost:8000/api/admin/collectors \
  -H 'Content-Type: application/json' \
  -d '{"code":"scrapy-news","name":"Scrapy News","collector_type":"scrapy","config":{"url":"https://example.com","spider_name":"example_news","results":[{"url":"https://example.com/news/2","title":"Scraped News"}]}}'

curl -X POST http://localhost:8000/api/admin/collectors \
  -H 'Content-Type: application/json' \
  -d '{"code":"dynamic-news","name":"Dynamic News","collector_type":"playwright","config":{"url":"https://example.com/dynamic","enabled":true,"links":["https://example.com/dynamic/article"]}}'
```

上传文件：

```bash
curl -X POST http://localhost:8000/api/admin/uploads \
  -F 'file=@./report.md' \
  -F 'title=Report'
```

直接执行一次采集任务：

```bash
curl -X POST http://localhost:8000/api/admin/crawl-jobs/{job_id}/run
```

查询任务与文档：

```bash
curl http://localhost:8000/api/admin/crawl-jobs/{job_id}
curl 'http://localhost:8000/api/documents?keyword=example'
curl http://localhost:8000/api/documents/{document_id}/versions
curl http://localhost:8000/api/documents/{document_id}/artifacts
curl http://localhost:8000/api/artifacts/{artifact_id}
```

审核文档和抽取版本：

```bash
curl -X POST http://localhost:8000/api/documents/{document_id}/review \
  -H 'Content-Type: application/json' \
  -d '{"status":"reviewed","reviewer":"alice","reason":"首轮审核通过"}'

curl -X POST http://localhost:8000/api/documents/{document_id}/versions/{version_id}/review \
  -H 'Content-Type: application/json' \
  -d '{"extraction_status":"reviewed","reviewer":"alice","reason":"正文抽取可用"}'
```

创建待分发到 `knowledge-app` 的记录：

```bash
curl -X POST http://localhost:8000/api/admin/distributions/knowledge \
  -H 'Content-Type: application/json' \
  -d '{"document_version_id":"00000000-0000-0000-0000-000000000000","target_dataset":"default","dispatch":false}'
```

查询、对账和重试分发记录：

```bash
curl 'http://localhost:8000/api/admin/distributions?status=failed'

curl -X POST http://localhost:8000/api/admin/distributions/{distribution_id}/status \
  -H 'Content-Type: application/json' \
  -d '{"status":"failed","last_error":"knowledge-app timeout","metadata":{"remote_id":"abc"}}'

curl -X POST http://localhost:8000/api/admin/distributions/{distribution_id}/retry
```

配置 `knowledge-app` ingestion API 后，可手动触发投递：

```text
KNOWLEDGE_APP_INGEST_URL=http://knowledge-admin-backend:8000/api/internal/ingestions
KNOWLEDGE_APP_API_KEY=optional-shared-secret
KNOWLEDGE_APP_TIMEOUT_SECONDS=20
```

```bash
curl -X POST http://localhost:8000/api/admin/distributions/{distribution_id}/dispatch
```

也可在创建记录时设置 `"dispatch": true`。Celery broker 可用时会投递后台任务；
未配置 Celery 时同步执行一次。未配置 `KNOWLEDGE_APP_INGEST_URL` 时记录保持
`pending`，并在 `last_error` / `payload.status_history` 里标记跳过原因。

## 5. Celery

配置 `CELERY_BROKER_URL` 后，`POST /api/admin/crawl-jobs` 默认投递
`app.tasks.crawl_url`。未配置 Celery 时，API 只创建 `pending` 任务，可通过
`POST /api/admin/crawl-jobs/{job_id}/run` 手动同步执行，方便本地验证。

平台 RabbitMQ 已验证默认队列路由：`info.admin.default` 使用同名 direct exchange
和 routing key。验证 job `a14ebe20-2bf1-422a-8637-fc9178ebff9c` 由 API 投递后被
本地 worker 消费，采集成功，并继续投递/执行 `app.tasks.index_document_version`。

## 6. 当前边界

- 已实现静态 HTML 拉取、原始证据保存、正文抽取和版本治理。
- 已实现 RSS/Atom feed 发现，并将条目转为待采集 `crawl_job`。
- 已实现官方 API JSON 列表发现，并将条目转为待采集 `crawl_job`。
- 已实现 changedetection 触发器入口，并将变化触发转为待采集 `crawl_job`。
- 已实现文件上传入口；文本/HTML/Markdown 直接入库，PDF/Office 标记为 `pending_tool_processing`。
- 已预留 S3 写入；本地默认使用文件 fallback。
- 已实现 artifact 元数据查询和基础标题/URL 搜索。
- 已实现来源治理字段：`trust_level`、`copyright_status`、`license_url`、`terms_url`，用于记录来源可信度和版权状态。
- 已实现轻量近似重复检测：正文入库时写入 `metadata_json.content_fingerprint`、`duplicate_state` 和候选列表，只做提示不自动合并。
- 已实现文档关系标注 API：可记录转载、同源故事和 canonical 候选，写入 `metadata_json.document_relations`，不做物理合并。
- 已实现实体/主题关联标注 API：可记录公司、证券、行业、主题到 `metadata_json.entity_links`，并保留标注历史。
- 已实现摘要、标签和重要性评分标注 API：写入 `metadata_json.summary_profile`，并保留 `summary_history`。
- 已实现 Info App `information` 搜索索引 mapping、Elasticsearch/OpenSearch 写入 adapter 和手动重建入口。
- 已实现 `document_version` 创建成功后的搜索索引增量写入；Celery 可用时后台执行，未配置时主事务提交后 best-effort 执行。
- 搜索 adapter 已支持平台注入的 `ELASTICSEARCH_USERNAME`、`ELASTICSEARCH_PASSWORD`、`ELASTICSEARCH_CA_CERT_PATH` 和 `ELASTICSEARCH_ALIASES`，会优先写入 `information.write` alias。
- 已通过平台 Elasticsearch Secret/CA 和 `development-info-app-information-write` alias 验证真实写入权限；验证文档写入后已删除。
- 已通过平台 RabbitMQ 队列验证 `crawl_url -> document_version -> index_document_version` 后台任务链路。
- 已实现 `knowledge-app` 分发记录、payload 生成、状态对账、失败重试和可配置 ingestion API 投递。
- 已实现文档和抽取版本审核状态调整，审核记录保存在 `metadata_json.review_history`。
- 已在本机 kind PostgreSQL / Redis 和本地对象存储配置下执行 migration，并通过本机 HTTP 页面验证同步 crawl job 成功路径。
- 已实现 Scrapy 和 Playwright 外部结果导入 adapter；真实 Scrapy/Playwright 执行仍由后续专用 crawler worker 承担。
- 已新增管理前端最小页面 `info-admin-frontend/src/pages/info/crawl.vue`，并通过 `pnpm type-check` 与 `pnpm build-only` 验证。
- 当前本机外网抓取 `https://example.com` 超时，API 已能记录为 crawl job 业务失败，不再返回 500。
- 尚未实现完整反爬策略。
