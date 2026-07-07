from __future__ import annotations

import hashlib
import importlib.metadata
import logging
import time
import uuid
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.collectors import get_collector_adapter
from app.infrastructure.models.info import (
    CrawlJob,
    DistributionRecord,
    ExtractedContent,
    InfoCollector,
    InfoDocument,
    InfoDocumentVersion,
    InfoSource,
    RawArtifact,
)
from app.infrastructure.external.knowledge_app import (
    KnowledgeAppNotConfiguredError,
    get_knowledge_app_client,
)
from app.infrastructure.storage.object_storage import (
    StoredObject,
    get_object_storage,
    make_artifact_key,
)
from app.infrastructure.search import build_info_index_document, get_info_search_index
from core.config import get_settings

logger = logging.getLogger(__name__)

try:
    import trafilatura
except ImportError:  # pragma: no cover - allows code import before optional deps install
    trafilatura = None


def _now() -> datetime:
    return datetime.now(UTC)


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _extract_html(html: str, url: str) -> tuple[str, str, str, datetime | None]:
    if trafilatura is None:
        title = url
        text = html[:5000]
        return title, f"# {title}\n\n{text}", text, None

    metadata = trafilatura.extract_metadata(html)
    title = metadata.title if metadata and metadata.title else url
    published_at = None
    if metadata and metadata.date:
        try:
            parsed = parsedate_to_datetime(metadata.date)
            published_at = parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
        except (TypeError, ValueError):
            published_at = None

    markdown_candidate = trafilatura.extract(
        html,
        url=url,
        output_format="markdown",
        include_links=True,
        include_tables=True,
    )
    text_candidate = trafilatura.extract(html, url=url, output_format="txt")
    if not markdown_candidate and not text_candidate:
        raise ValueError("trafilatura returned empty content")
    markdown = markdown_candidate or text_candidate or ""
    text = text_candidate or markdown
    return title, markdown, text, published_at


def _trafilatura_version() -> str | None:
    if trafilatura is None:
        return None
    try:
        return importlib.metadata.version("trafilatura")
    except importlib.metadata.PackageNotFoundError:
        return None


def _source_code(source: InfoSource | None) -> str:
    return source.code if source else "manual"


async def create_source(
    session: AsyncSession,
    *,
    code: str,
    name: str,
    source_type: str,
    base_url: str | None,
    description: str | None,
) -> InfoSource:
    source = InfoSource(
        code=code,
        name=name,
        source_type=source_type,
        base_url=base_url,
        description=description,
    )
    session.add(source)
    await session.commit()
    await session.refresh(source)
    return source


async def list_sources(session: AsyncSession) -> list[InfoSource]:
    result = await session.execute(select(InfoSource).order_by(InfoSource.created_at.desc()))
    return list(result.scalars())


async def create_collector(
    session: AsyncSession,
    *,
    code: str,
    name: str,
    collector_type: str,
    source_id: uuid.UUID | None,
    config: dict,
) -> InfoCollector:
    collector = InfoCollector(
        source_id=source_id,
        code=code,
        name=name,
        collector_type=collector_type,
        config=config,
    )
    session.add(collector)
    await session.commit()
    await session.refresh(collector)
    return collector


async def list_collectors(session: AsyncSession) -> list[InfoCollector]:
    result = await session.execute(
        select(InfoCollector).order_by(InfoCollector.created_at.desc())
    )
    return list(result.scalars())


async def run_collector_discovery(
    session: AsyncSession,
    *,
    collector_id: uuid.UUID,
    url: str | None = None,
) -> list[CrawlJob]:
    collector = await session.get(InfoCollector, collector_id)
    if collector is None:
        raise ValueError(f"collector not found: {collector_id}")
    target_url = url or collector.config.get("url") or collector.config.get("feed_url")
    if not target_url:
        raise ValueError("collector discovery requires a url or config.url")
    adapter = get_collector_adapter(collector.collector_type)
    links = await adapter.discover(url=target_url, config=collector.config)
    jobs = [
        CrawlJob(
            source_id=collector.source_id,
            collector_id=collector.id,
            job_type="url",
            target_url=link.url,
            status="pending",
            request={
                "collector_type": collector.collector_type,
                "title": link.title,
                "summary": link.summary,
                "published_at": link.published_at.isoformat()
                if link.published_at
                else None,
                "metadata": link.metadata,
            },
        )
        for link in links
    ]
    session.add_all(jobs)
    await session.commit()
    for job in jobs:
        await session.refresh(job)
    return jobs


async def ingest_uploaded_file(
    session: AsyncSession,
    *,
    filename: str,
    content: bytes,
    content_type: str,
    source_id: uuid.UUID | None = None,
    title: str | None = None,
) -> InfoDocumentVersion:
    storage = get_object_storage()
    source = await session.get(InfoSource, source_id) if source_id else None
    source_code = _source_code(source)
    job = CrawlJob(
        source_id=source_id,
        job_type="upload",
        target_url=f"upload://{filename}",
        status="succeeded",
        started_at=_now(),
        finished_at=_now(),
        request={"filename": filename, "content_type": content_type},
    )
    session.add(job)
    await session.flush()
    date_path = _now().strftime("%Y-%m-%d")
    raw = storage.put_bytes(
        object_key=make_artifact_key(
            source_code=source_code,
            date_path=date_path,
            job_id=str(job.id),
            artifact_name=filename,
        ),
        data=content,
        content_type=content_type,
        metadata={"crawl_job_id": str(job.id), "artifact_type": "uploaded_file"},
    )
    raw_artifact = _artifact(job, raw, "uploaded_file")
    session.add(raw_artifact)
    await session.flush()

    display_title = title or filename
    source_url = f"upload://{filename}"
    text = _decode_upload_text(content, content_type, filename)
    if text is None:
        content_hash = _hash_bytes(content)
        document = await _find_or_create_document(
            session=session,
            source=source,
            url=source_url,
            title=display_title,
            published_at=None,
            content_hash=content_hash,
        )
        version = InfoDocumentVersion(
            document_id=document.id,
            version_no=await _next_version_no(session, document.id),
            source_url=source_url,
            title=display_title,
            content_hash=content_hash,
            raw_artifact_id=raw_artifact.id,
            extraction_status="pending_tool_processing",
            metadata_json={"content_type": content_type, "filename": filename},
        )
        session.add(version)
        await session.flush()
        raw_artifact.document_id = document.id
        raw_artifact.document_version_id = version.id
        document.current_version_id = version.id
        document.content_hash = content_hash
        job.document_id = document.id
        job.document_version_id = version.id
        await session.commit()
        await _enqueue_or_index_document_version(session, version.id)
        await session.refresh(version)
        return version

    markdown = text if content_type in ("text/markdown", "text/x-markdown") else f"# {display_title}\n\n{text}"
    content_hash = _hash_text(text)
    document = await _find_or_create_document(
        session=session,
        source=source,
        url=source_url,
        title=display_title,
        published_at=None,
        content_hash=content_hash,
    )
    clean = storage.put_bytes(
        object_key=make_artifact_key(
            source_code=source_code,
            date_path=date_path,
            job_id=str(job.id),
            artifact_name="clean.md",
        ),
        data=markdown.encode("utf-8"),
        content_type="text/markdown; charset=utf-8",
    )
    text_obj = storage.put_bytes(
        object_key=make_artifact_key(
            source_code=source_code,
            date_path=date_path,
            job_id=str(job.id),
            artifact_name="text.txt",
        ),
        data=text.encode("utf-8"),
        content_type="text/plain; charset=utf-8",
    )
    clean_artifact = _artifact(job, clean, "clean_markdown", document_id=document.id)
    text_artifact = _artifact(job, text_obj, "text_plain", document_id=document.id)
    session.add_all([clean_artifact, text_artifact])
    await session.flush()
    version = InfoDocumentVersion(
        document_id=document.id,
        version_no=await _next_version_no(session, document.id),
        source_url=source_url,
        title=display_title,
        content_hash=content_hash,
        raw_artifact_id=raw_artifact.id,
        clean_artifact_id=clean_artifact.id,
        text_artifact_id=text_artifact.id,
        extraction_status="succeeded",
        extractor_name="upload-text",
        metadata_json={"content_type": content_type, "filename": filename},
    )
    session.add(version)
    await session.flush()
    session.add_all(
        [
            ExtractedContent(
                document_version_id=version.id,
                content_format="markdown",
                bucket=clean.bucket,
                object_key=clean.object_key,
                sha256=clean.sha256,
                size_bytes=clean.size_bytes,
                extractor_name="upload-text",
                metadata_json={},
            ),
            ExtractedContent(
                document_version_id=version.id,
                content_format="text",
                bucket=text_obj.bucket,
                object_key=text_obj.object_key,
                sha256=text_obj.sha256,
                size_bytes=text_obj.size_bytes,
                extractor_name="upload-text",
                metadata_json={},
            ),
        ]
    )
    raw_artifact.document_id = document.id
    raw_artifact.document_version_id = version.id
    clean_artifact.document_version_id = version.id
    text_artifact.document_version_id = version.id
    document.current_version_id = version.id
    document.content_hash = content_hash
    job.document_id = document.id
    job.document_version_id = version.id
    await session.commit()
    await _enqueue_or_index_document_version(session, version.id)
    await session.refresh(version)
    return version


async def create_crawl_job(
    session: AsyncSession,
    *,
    target_url: str,
    source_id: uuid.UUID | None = None,
) -> CrawlJob:
    job = CrawlJob(source_id=source_id, target_url=target_url, status="pending")
    session.add(job)
    await session.commit()
    await session.refresh(job)
    return job


async def get_crawl_job(session: AsyncSession, job_id: uuid.UUID) -> CrawlJob | None:
    return await session.get(CrawlJob, job_id)


async def list_documents(session: AsyncSession, limit: int, offset: int) -> list[InfoDocument]:
    result = await session.execute(
        select(InfoDocument)
        .order_by(InfoDocument.updated_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars())


async def search_documents(
    session: AsyncSession,
    *,
    keyword: str | None,
    source_id: uuid.UUID | None,
    status: str | None,
    limit: int,
    offset: int,
) -> list[InfoDocument]:
    query = select(InfoDocument)
    if keyword:
        pattern = f"%{keyword}%"
        query = query.where(
            (InfoDocument.title.ilike(pattern))
            | (InfoDocument.canonical_url.ilike(pattern))
        )
    if source_id:
        query = query.where(InfoDocument.source_id == source_id)
    if status:
        query = query.where(InfoDocument.status == status)
    result = await session.execute(
        query.order_by(InfoDocument.updated_at.desc()).limit(limit).offset(offset)
    )
    return list(result.scalars())


async def get_document(session: AsyncSession, document_id: uuid.UUID) -> InfoDocument | None:
    return await session.get(InfoDocument, document_id)


def _review_metadata(
    *,
    existing: dict | None,
    status: str,
    reviewer: str | None,
    reason: str | None,
) -> dict:
    metadata = dict(existing or {})
    history = list(metadata.get("review_history") or [])
    history.append(
        {
            "status": status,
            "reviewer": reviewer,
            "reason": reason,
            "reviewed_at": _now().isoformat(),
        }
    )
    metadata["review_history"] = history
    metadata["last_review"] = history[-1]
    return metadata


async def review_document(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    status: str,
    reviewer: str | None,
    reason: str | None,
) -> InfoDocument:
    document = await session.get(InfoDocument, document_id)
    if document is None:
        raise ValueError(f"document not found: {document_id}")
    document.status = status
    document.metadata_json = _review_metadata(
        existing=document.metadata_json,
        status=status,
        reviewer=reviewer,
        reason=reason,
    )
    await session.commit()
    await session.refresh(document)
    return document


async def list_document_versions(
    session: AsyncSession, document_id: uuid.UUID
) -> list[InfoDocumentVersion]:
    result = await session.execute(
        select(InfoDocumentVersion)
        .where(InfoDocumentVersion.document_id == document_id)
        .order_by(InfoDocumentVersion.version_no.desc())
    )
    return list(result.scalars())


async def review_document_version(
    session: AsyncSession,
    *,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    extraction_status: str,
    reviewer: str | None,
    reason: str | None,
) -> InfoDocumentVersion:
    version = await session.get(InfoDocumentVersion, version_id)
    if version is None or version.document_id != document_id:
        raise ValueError(f"document version not found: {version_id}")
    version.extraction_status = extraction_status
    version.metadata_json = _review_metadata(
        existing=version.metadata_json,
        status=extraction_status,
        reviewer=reviewer,
        reason=reason,
    )
    await session.commit()
    await session.refresh(version)
    return version


async def get_artifact(session: AsyncSession, artifact_id: uuid.UUID) -> RawArtifact | None:
    return await session.get(RawArtifact, artifact_id)


async def list_artifacts(
    session: AsyncSession,
    *,
    crawl_job_id: uuid.UUID | None = None,
    document_id: uuid.UUID | None = None,
    document_version_id: uuid.UUID | None = None,
) -> list[RawArtifact]:
    query = select(RawArtifact)
    if crawl_job_id:
        query = query.where(RawArtifact.crawl_job_id == crawl_job_id)
    if document_id:
        query = query.where(RawArtifact.document_id == document_id)
    if document_version_id:
        query = query.where(RawArtifact.document_version_id == document_version_id)
    result = await session.execute(query.order_by(RawArtifact.created_at.asc()))
    return list(result.scalars())


async def create_knowledge_distribution(
    session: AsyncSession,
    *,
    document_version_id: uuid.UUID,
    target_dataset: str | None = None,
) -> DistributionRecord:
    version = await session.get(InfoDocumentVersion, document_version_id)
    if version is None:
        raise ValueError(f"document version not found: {document_version_id}")
    document = await session.get(InfoDocument, version.document_id)
    if document is None:
        raise ValueError(f"document not found: {version.document_id}")
    payload = {
        "document_id": str(document.id),
        "version_id": str(version.id),
        "content_hash": version.content_hash,
        "title": version.title,
        "source_url": version.source_url,
        "source_name": document.source_name,
        "published_at": document.published_at.isoformat()
        if document.published_at
        else None,
        "clean_artifact_id": str(version.clean_artifact_id)
        if version.clean_artifact_id
        else None,
        "text_artifact_id": str(version.text_artifact_id)
        if version.text_artifact_id
        else None,
        "metadata": document.metadata_json,
    }
    record = DistributionRecord(
        document_id=document.id,
        document_version_id=version.id,
        target_app="knowledge-app",
        target_dataset=target_dataset,
        content_hash=version.content_hash,
        status="pending",
        payload=payload,
    )
    session.add(record)
    await session.commit()
    await session.refresh(record)
    return record


async def list_distributions(
    session: AsyncSession,
    *,
    document_version_id: uuid.UUID | None,
    target_app: str | None,
    status: str | None,
    limit: int,
    offset: int,
) -> list[DistributionRecord]:
    query = select(DistributionRecord)
    if document_version_id:
        query = query.where(DistributionRecord.document_version_id == document_version_id)
    if target_app:
        query = query.where(DistributionRecord.target_app == target_app)
    if status:
        query = query.where(DistributionRecord.status == status)
    result = await session.execute(
        query.order_by(DistributionRecord.updated_at.desc()).limit(limit).offset(offset)
    )
    return list(result.scalars())


async def get_distribution(
    session: AsyncSession, distribution_id: uuid.UUID
) -> DistributionRecord | None:
    return await session.get(DistributionRecord, distribution_id)


def _distribution_status_payload(
    *,
    existing: dict | None,
    status: str,
    last_error: str | None,
    metadata: dict | None,
) -> dict:
    payload = dict(existing or {})
    history = list(payload.get("status_history") or [])
    history.append(
        {
            "status": status,
            "last_error": last_error,
            "metadata": metadata or {},
            "updated_at": _now().isoformat(),
        }
    )
    payload["status_history"] = history
    payload["last_status_update"] = history[-1]
    return payload


def _knowledge_ingestion_payload(record: DistributionRecord) -> dict:
    internal_keys = {
        "status_history",
        "last_status_update",
        "retry_history",
        "last_retry",
    }
    payload = {
        key: value
        for key, value in dict(record.payload or {}).items()
        if key not in internal_keys
    }
    payload["distribution_id"] = str(record.id)
    payload["target_dataset"] = record.target_dataset
    return payload


async def update_distribution_status(
    session: AsyncSession,
    *,
    distribution_id: uuid.UUID,
    status: str,
    last_error: str | None,
    metadata: dict | None = None,
) -> DistributionRecord:
    record = await session.get(DistributionRecord, distribution_id)
    if record is None:
        raise ValueError(f"distribution not found: {distribution_id}")
    record.status = status
    record.last_error = last_error
    record.payload = _distribution_status_payload(
        existing=record.payload,
        status=status,
        last_error=last_error,
        metadata=metadata,
    )
    await session.commit()
    await session.refresh(record)
    return record


async def dispatch_distribution(
    session: AsyncSession,
    *,
    distribution_id: uuid.UUID,
) -> DistributionRecord:
    record = await session.get(DistributionRecord, distribution_id)
    if record is None:
        raise ValueError(f"distribution not found: {distribution_id}")
    if record.target_app != "knowledge-app":
        raise ValueError(f"unsupported distribution target: {record.target_app}")
    if record.status == "succeeded":
        return record

    record.status = "running"
    record.last_error = None
    record.payload = _distribution_status_payload(
        existing=record.payload,
        status="running",
        last_error=None,
        metadata={"target_app": record.target_app},
    )
    await session.commit()
    await session.refresh(record)

    try:
        response = await get_knowledge_app_client().ingest_document(
            _knowledge_ingestion_payload(record)
        )
    except KnowledgeAppNotConfiguredError as exc:
        record.status = "pending"
        record.last_error = str(exc)
        record.payload = _distribution_status_payload(
            existing=record.payload,
            status="pending",
            last_error=str(exc),
            metadata={"skipped": True, "reason": "not_configured"},
        )
    except Exception as exc:
        record.status = "failed"
        record.last_error = str(exc)
        record.payload = _distribution_status_payload(
            existing=record.payload,
            status="failed",
            last_error=str(exc),
            metadata={"target_app": record.target_app},
        )
    else:
        record.status = "succeeded"
        record.last_error = None
        record.payload = _distribution_status_payload(
            existing=record.payload,
            status="succeeded",
            last_error=None,
            metadata={"target_app": record.target_app, "response": response},
        )

    await session.commit()
    await session.refresh(record)
    return record


def _distribution_retry_payload(existing: dict | None, *, previous_error: str | None) -> dict:
    payload = dict(existing or {})
    history = list(payload.get("retry_history") or [])
    history.append(
        {
            "previous_error": previous_error,
            "retried_at": _now().isoformat(),
        }
    )
    payload["retry_history"] = history
    payload["last_retry"] = history[-1]
    return payload


async def retry_distribution(
    session: AsyncSession, *, distribution_id: uuid.UUID
) -> DistributionRecord:
    record = await session.get(DistributionRecord, distribution_id)
    if record is None:
        raise ValueError(f"distribution not found: {distribution_id}")
    if record.status != "failed":
        raise ValueError("only failed distributions can be retried")
    record.payload = _distribution_retry_payload(
        record.payload, previous_error=record.last_error
    )
    record.status = "pending"
    record.last_error = None
    await session.commit()
    await session.refresh(record)
    return record


async def index_document_version(
    session: AsyncSession, *, document_version_id: uuid.UUID
) -> dict:
    search_index = get_info_search_index()
    result = {
        "enabled": search_index.enabled,
        "index_name": search_index.index_name,
        "index_created": False,
        "indexed": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
    }
    if not search_index.enabled:
        result["skipped"] = 1
        return result

    version = await session.get(InfoDocumentVersion, document_version_id)
    if version is None:
        result["skipped"] = 1
        result["errors"].append(f"document version not found: {document_version_id}")
        return result

    document = await session.get(InfoDocument, version.document_id)
    if document is None:
        result["skipped"] = 1
        result["errors"].append(f"document not found: {version.document_id}")
        return result

    artifacts = await list_artifacts(session, document_version_id=version.id)
    extracted_result = await session.execute(
        select(ExtractedContent).where(ExtractedContent.document_version_id == version.id)
    )
    payload = build_info_index_document(
        document=document,
        version=version,
        artifacts=artifacts,
        extracted_contents=list(extracted_result.scalars()),
    )
    try:
        result["index_created"] = await search_index.ensure_index()
        await search_index.index_document(document_id=str(version.id), payload=payload)
        result["indexed"] = 1
    except Exception as exc:
        result["failed"] = 1
        result["errors"].append(f"{version.id}: {exc}")
    return result


async def _enqueue_or_index_document_version(
    session: AsyncSession, document_version_id: uuid.UUID
) -> None:
    try:
        from app.infrastructure.messaging.celery_producer import get_celery_producer

        producer = get_celery_producer()
        if producer.enabled:
            producer.dispatch_index_document_version(document_version_id)
            return
    except Exception as exc:
        logger.warning(
            "document_version search indexing task dispatch failed; falling back inline",
            extra={
                "document_version_id": str(document_version_id),
                "error": str(exc),
            },
        )

    result = await index_document_version(
        session, document_version_id=document_version_id
    )
    if result["failed"]:
        logger.warning(
            "document_version search indexing failed",
            extra={
                "document_version_id": str(document_version_id),
                "errors": result["errors"],
            },
        )


async def rebuild_search_index(session: AsyncSession, *, limit: int) -> dict:
    search_index = get_info_search_index()
    result = {
        "enabled": search_index.enabled,
        "index_name": search_index.index_name,
        "index_created": False,
        "indexed": 0,
        "skipped": 0,
        "failed": 0,
        "errors": [],
    }
    if not search_index.enabled:
        result["skipped"] = limit
        return result

    versions_result = await session.execute(
        select(InfoDocumentVersion)
        .order_by(InfoDocumentVersion.updated_at.desc())
        .limit(limit)
    )
    versions = list(versions_result.scalars())
    for version in versions:
        indexed = await index_document_version(
            session, document_version_id=version.id
        )
        result["index_created"] = result["index_created"] or indexed["index_created"]
        result["indexed"] += indexed["indexed"]
        result["skipped"] += indexed["skipped"]
        result["failed"] += indexed["failed"]
        result["errors"].extend(indexed["errors"])
    return result


async def process_crawl_job(session: AsyncSession, job_id: uuid.UUID) -> CrawlJob:
    settings = get_settings()
    storage = get_object_storage()
    job = await session.get(CrawlJob, job_id)
    if job is None:
        raise ValueError(f"crawl job not found: {job_id}")

    source = await session.get(InfoSource, job.source_id) if job.source_id else None
    source_code = _source_code(source)
    date_path = _now().strftime("%Y-%m-%d")
    started = time.perf_counter()
    job.status = "running"
    job.started_at = _now()
    job.attempt_count += 1
    await session.commit()
    raw_artifact: RawArtifact | None = None
    headers_artifact: RawArtifact | None = None
    final_url = job.target_url
    version_to_index_id: uuid.UUID | None = None

    try:
        async with httpx.AsyncClient(
            timeout=settings.crawl_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": settings.crawl_user_agent},
        ) as client:
            response = await client.get(job.target_url)
            content = await response.aread()
        if len(content) > settings.crawl_max_bytes:
            raise ValueError(f"response too large: {len(content)} bytes")

        job.http_status = response.status_code
        job.final_url = str(response.url)
        final_url = job.final_url or job.target_url
        job.response_metadata = {
            "headers": dict(response.headers),
            "encoding": response.encoding,
        }

        raw = storage.put_bytes(
            object_key=make_artifact_key(
                source_code=source_code,
                date_path=date_path,
                job_id=str(job.id),
                artifact_name="raw.html",
            ),
            data=content,
            content_type=response.headers.get("content-type", "text/html"),
            metadata={"crawl_job_id": str(job.id), "artifact_type": "raw_html"},
        )
        headers = storage.put_json(
            object_key=make_artifact_key(
                source_code=source_code,
                date_path=date_path,
                job_id=str(job.id),
                artifact_name="headers.json",
            ),
            payload=dict(response.headers),
        )
        raw_artifact = _artifact(job, raw, "raw_html")
        headers_artifact = _artifact(job, headers, "headers_json")
        session.add_all([raw_artifact, headers_artifact])
        await session.flush()

        if response.status_code >= 400:
            raise ValueError(f"http status {response.status_code}")

        html = content.decode(response.encoding or "utf-8", errors="replace")
        title, markdown, text, published_at = _extract_html(html, job.final_url or job.target_url)
        content_hash = _hash_text(text)
        document = await _find_or_create_document(
            session=session,
            source=source,
            url=job.final_url or job.target_url,
            title=title,
            published_at=published_at,
            content_hash=content_hash,
        )
        next_version = await _next_version_no(session, document.id)
        if document.content_hash == content_hash and document.current_version_id:
            job.status = "succeeded"
            job.document_id = document.id
            job.document_version_id = document.current_version_id
            job.finished_at = _now()
            job.duration_ms = int((time.perf_counter() - started) * 1000)
            await session.commit()
            await session.refresh(job)
            return job

        clean = storage.put_bytes(
            object_key=make_artifact_key(
                source_code=source_code,
                date_path=date_path,
                job_id=str(job.id),
                artifact_name="clean.md",
            ),
            data=markdown.encode("utf-8"),
            content_type="text/markdown; charset=utf-8",
            metadata={"crawl_job_id": str(job.id), "artifact_type": "clean_markdown"},
        )
        text_obj = storage.put_bytes(
            object_key=make_artifact_key(
                source_code=source_code,
                date_path=date_path,
                job_id=str(job.id),
                artifact_name="text.txt",
            ),
            data=text.encode("utf-8"),
            content_type="text/plain; charset=utf-8",
            metadata={"crawl_job_id": str(job.id), "artifact_type": "text_plain"},
        )
        clean_artifact = _artifact(job, clean, "clean_markdown", document_id=document.id)
        text_artifact = _artifact(job, text_obj, "text_plain", document_id=document.id)
        session.add_all([clean_artifact, text_artifact])
        await session.flush()

        version = InfoDocumentVersion(
            document_id=document.id,
            version_no=next_version,
            source_url=job.final_url or job.target_url,
            title=title,
            content_hash=content_hash,
            raw_artifact_id=raw_artifact.id,
            clean_artifact_id=clean_artifact.id,
            text_artifact_id=text_artifact.id,
            extractor_name="trafilatura" if trafilatura else "fallback-html",
            extractor_version=_trafilatura_version(),
            metadata_json={"published_at_candidate": published_at.isoformat() if published_at else None},
        )
        session.add(version)
        await session.flush()
        session.add_all(
            [
                ExtractedContent(
                    document_version_id=version.id,
                    content_format="markdown",
                    bucket=clean.bucket,
                    object_key=clean.object_key,
                    sha256=clean.sha256,
                    size_bytes=clean.size_bytes,
                    extractor_name=version.extractor_name,
                    metadata_json={},
                ),
                ExtractedContent(
                    document_version_id=version.id,
                    content_format="text",
                    bucket=text_obj.bucket,
                    object_key=text_obj.object_key,
                    sha256=text_obj.sha256,
                    size_bytes=text_obj.size_bytes,
                    extractor_name=version.extractor_name,
                    metadata_json={},
                ),
            ]
        )

        raw_artifact.document_id = document.id
        raw_artifact.document_version_id = version.id
        headers_artifact.document_id = document.id
        headers_artifact.document_version_id = version.id
        clean_artifact.document_version_id = version.id
        text_artifact.document_version_id = version.id
        document.title = title
        document.published_at = published_at or document.published_at
        document.current_version_id = version.id
        document.content_hash = content_hash
        job.status = "succeeded"
        job.document_id = document.id
        job.document_version_id = version.id
        version_to_index_id = version.id
    except Exception as exc:
        if raw_artifact is not None and job.http_status and job.http_status < 400:
            failed_document, failed_version = await _record_extraction_failure(
                session=session,
                source=source,
                url=final_url,
                job=job,
                raw_artifact=raw_artifact,
                headers_artifact=headers_artifact,
                error=exc,
            )
            job.document_id = failed_document.id
            job.document_version_id = failed_version.id
            version_to_index_id = failed_version.id
        job.status = "failed"
        job.error_code = exc.__class__.__name__
        job.error_message = str(exc)
    finally:
        job.finished_at = _now()
        job.duration_ms = int((time.perf_counter() - started) * 1000)
        await session.commit()
        await session.refresh(job)
        if version_to_index_id is not None:
            await _enqueue_or_index_document_version(session, version_to_index_id)
    return job


async def _record_extraction_failure(
    *,
    session: AsyncSession,
    source: InfoSource | None,
    url: str,
    job: CrawlJob,
    raw_artifact: RawArtifact,
    headers_artifact: RawArtifact | None,
    error: Exception,
) -> tuple[InfoDocument, InfoDocumentVersion]:
    content_hash = raw_artifact.sha256
    document = await _find_or_create_document(
        session=session,
        source=source,
        url=url,
        title=url,
        published_at=None,
        content_hash=content_hash,
    )
    next_version = await _next_version_no(session, document.id)
    version = InfoDocumentVersion(
        document_id=document.id,
        version_no=next_version,
        source_url=url,
        title=url,
        content_hash=content_hash,
        raw_artifact_id=raw_artifact.id,
        extraction_status="extraction_failed",
        extractor_name="trafilatura" if trafilatura else "fallback-html",
        extractor_version=_trafilatura_version(),
        metadata_json={
            "error_code": error.__class__.__name__,
            "error_message": str(error),
        },
    )
    session.add(version)
    await session.flush()
    raw_artifact.document_id = document.id
    raw_artifact.document_version_id = version.id
    if headers_artifact is not None:
        headers_artifact.document_id = document.id
        headers_artifact.document_version_id = version.id
    document.current_version_id = version.id
    document.content_hash = content_hash
    return document, version


def _artifact(
    job: CrawlJob,
    stored: StoredObject,
    artifact_type: str,
    document_id: uuid.UUID | None = None,
) -> RawArtifact:
    return RawArtifact(
        crawl_job_id=job.id,
        document_id=document_id,
        artifact_type=artifact_type,
        bucket=stored.bucket,
        object_key=stored.object_key,
        version_id=stored.version_id,
        sha256=stored.sha256,
        size_bytes=stored.size_bytes,
        content_type=stored.content_type,
    )


def _decode_upload_text(
    content: bytes,
    content_type: str,
    filename: str,
) -> str | None:
    lowered = filename.lower()
    if (
        content_type.startswith("text/")
        or content_type in ("application/json", "application/xml")
        or lowered.endswith((".txt", ".md", ".markdown", ".html", ".htm", ".json", ".xml"))
    ):
        return content.decode("utf-8", errors="replace")
    return None


async def _find_or_create_document(
    *,
    session: AsyncSession,
    source: InfoSource | None,
    url: str,
    title: str,
    published_at: datetime | None,
    content_hash: str,
) -> InfoDocument:
    result = await session.execute(select(InfoDocument).where(InfoDocument.canonical_url == url))
    document = result.scalar_one_or_none()
    if document:
        return document
    document = InfoDocument(
        source_id=source.id if source else None,
        canonical_url=url,
        title=title,
        source_name=source.name if source else None,
        published_at=published_at,
        content_hash=content_hash,
        metadata_json={},
    )
    session.add(document)
    await session.flush()
    return document


async def _next_version_no(session: AsyncSession, document_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.max(InfoDocumentVersion.version_no)).where(
            InfoDocumentVersion.document_id == document_id
        )
    )
    current = result.scalar_one_or_none()
    return int(current or 0) + 1
