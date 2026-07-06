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

## 3. API

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
  -d '{"document_version_id":"00000000-0000-0000-0000-000000000000","target_dataset":"default"}'
```

## 4. Celery

配置 `CELERY_BROKER_URL` 后，`POST /api/admin/crawl-jobs` 默认投递
`app.tasks.crawl_url`。未配置 Celery 时，API 只创建 `pending` 任务，可通过
`POST /api/admin/crawl-jobs/{job_id}/run` 手动同步执行，方便本地验证。

## 5. 当前边界

- 已实现静态 HTML 拉取、原始证据保存、正文抽取和版本治理。
- 已实现 RSS/Atom feed 发现，并将条目转为待采集 `crawl_job`。
- 已实现官方 API JSON 列表发现，并将条目转为待采集 `crawl_job`。
- 已实现 changedetection 触发器入口，并将变化触发转为待采集 `crawl_job`。
- 已实现文件上传入口；文本/HTML/Markdown 直接入库，PDF/Office 标记为 `pending_tool_processing`。
- 已预留 S3 写入；本地默认使用文件 fallback。
- 已实现 artifact 元数据查询和基础标题/URL 搜索。
- 已实现 `knowledge-app` 分发记录与 payload 生成，但尚未调用 `knowledge-app` API。
- 已实现文档和抽取版本审核状态调整，审核记录保存在 `metadata_json.review_history`。
- 已提供 Scrapy 和 Playwright adapter 占位；尚未执行真实 Scrapy/Playwright 采集。
- 已新增管理前端最小页面 `info-admin-frontend/src/pages/info/crawl.vue`，但因前端依赖安装未完成尚未构建验证。
- 尚未实现 Elasticsearch 索引、抽取结果审核和完整反爬策略。
