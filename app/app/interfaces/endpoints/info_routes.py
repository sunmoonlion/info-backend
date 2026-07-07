from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.services import info_crawl_service
from app.infrastructure.messaging.celery_producer import get_celery_producer
from app.infrastructure.storage.postgres import get_db_session
from app.interfaces.schemas.info import (
    CollectorCreate,
    CollectorDiscoverRequest,
    CollectorRead,
    CrawlJobCreate,
    CrawlJobRead,
    DistributionCreate,
    DistributionRead,
    DistributionStatusUpdate,
    DocumentReviewRequest,
    DocumentRead,
    DocumentVersionRead,
    DocumentVersionReviewRequest,
    RawArtifactRead,
    SearchIndexRebuildRead,
    SourceCreate,
    SourceRead,
    UploadIngestRead,
)

router = APIRouter(tags=["资讯采集"])


@router.post("/admin/sources", response_model=SourceRead, status_code=status.HTTP_201_CREATED)
async def create_source(
    payload: SourceCreate, session: AsyncSession = Depends(get_db_session)
):
    return await info_crawl_service.create_source(
        session,
        code=payload.code,
        name=payload.name,
        source_type=payload.source_type,
        base_url=payload.base_url,
        description=payload.description,
    )


@router.get("/admin/sources", response_model=list[SourceRead])
async def list_sources(session: AsyncSession = Depends(get_db_session)):
    return await info_crawl_service.list_sources(session)


@router.post(
    "/admin/collectors",
    response_model=CollectorRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_collector(
    payload: CollectorCreate, session: AsyncSession = Depends(get_db_session)
):
    return await info_crawl_service.create_collector(
        session,
        code=payload.code,
        name=payload.name,
        collector_type=payload.collector_type,
        source_id=payload.source_id,
        config=payload.config,
    )


@router.get("/admin/collectors", response_model=list[CollectorRead])
async def list_collectors(session: AsyncSession = Depends(get_db_session)):
    return await info_crawl_service.list_collectors(session)


@router.post(
    "/admin/collectors/{collector_id}/discover",
    response_model=list[CrawlJobRead],
)
async def discover_collector(
    collector_id: uuid.UUID,
    payload: CollectorDiscoverRequest,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        return await info_crawl_service.run_collector_discovery(
            session,
            collector_id=collector_id,
            url=str(payload.url) if payload.url else None,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/admin/crawl-jobs", response_model=CrawlJobRead, status_code=status.HTTP_201_CREATED)
async def create_crawl_job(
    payload: CrawlJobCreate, session: AsyncSession = Depends(get_db_session)
):
    job = await info_crawl_service.create_crawl_job(
        session,
        target_url=str(payload.target_url),
        source_id=payload.source_id,
    )
    if payload.enqueue:
        producer = get_celery_producer()
        if producer.enabled:
            producer.dispatch_crawl_url(job.id)
    return job


@router.get("/admin/crawl-jobs/{job_id}", response_model=CrawlJobRead)
async def get_crawl_job(job_id: uuid.UUID, session: AsyncSession = Depends(get_db_session)):
    job = await info_crawl_service.get_crawl_job(session, job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="crawl job not found")
    return job


@router.post("/admin/crawl-jobs/{job_id}/run", response_model=CrawlJobRead)
async def run_crawl_job(job_id: uuid.UUID, session: AsyncSession = Depends(get_db_session)):
    job = await info_crawl_service.process_crawl_job(session, job_id)
    return job


@router.get("/documents", response_model=list[DocumentRead])
async def list_documents(
    keyword: str | None = Query(default=None),
    source_id: uuid.UUID | None = Query(default=None),
    document_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db_session),
):
    if keyword or source_id or document_status:
        return await info_crawl_service.search_documents(
            session,
            keyword=keyword,
            source_id=source_id,
            status=document_status,
            limit=limit,
            offset=offset,
        )
    return await info_crawl_service.list_documents(session, limit=limit, offset=offset)


@router.get("/documents/{document_id}", response_model=DocumentRead)
async def get_document(document_id: uuid.UUID, session: AsyncSession = Depends(get_db_session)):
    document = await info_crawl_service.get_document(session, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    return document


@router.post("/documents/{document_id}/review", response_model=DocumentRead)
async def review_document(
    document_id: uuid.UUID,
    payload: DocumentReviewRequest,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        return await info_crawl_service.review_document(
            session,
            document_id=document_id,
            status=payload.status,
            reviewer=payload.reviewer,
            reason=payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/documents/{document_id}/versions", response_model=list[DocumentVersionRead])
async def list_document_versions(
    document_id: uuid.UUID, session: AsyncSession = Depends(get_db_session)
):
    return await info_crawl_service.list_document_versions(session, document_id)


@router.post(
    "/documents/{document_id}/versions/{version_id}/review",
    response_model=DocumentVersionRead,
)
async def review_document_version(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    payload: DocumentVersionReviewRequest,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        return await info_crawl_service.review_document_version(
            session,
            document_id=document_id,
            version_id=version_id,
            extraction_status=payload.extraction_status,
            reviewer=payload.reviewer,
            reason=payload.reason,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/artifacts/{artifact_id}", response_model=RawArtifactRead)
async def get_artifact(artifact_id: uuid.UUID, session: AsyncSession = Depends(get_db_session)):
    artifact = await info_crawl_service.get_artifact(session, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="artifact not found")
    return artifact


@router.get("/admin/crawl-jobs/{job_id}/artifacts", response_model=list[RawArtifactRead])
async def list_crawl_job_artifacts(
    job_id: uuid.UUID, session: AsyncSession = Depends(get_db_session)
):
    return await info_crawl_service.list_artifacts(session, crawl_job_id=job_id)


@router.get("/documents/{document_id}/artifacts", response_model=list[RawArtifactRead])
async def list_document_artifacts(
    document_id: uuid.UUID, session: AsyncSession = Depends(get_db_session)
):
    return await info_crawl_service.list_artifacts(session, document_id=document_id)


@router.get(
    "/documents/{document_id}/versions/{version_id}/artifacts",
    response_model=list[RawArtifactRead],
)
async def list_document_version_artifacts(
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    session: AsyncSession = Depends(get_db_session),
):
    document = await info_crawl_service.get_document(session, document_id)
    if document is None:
        raise HTTPException(status_code=404, detail="document not found")
    return await info_crawl_service.list_artifacts(
        session, document_version_id=version_id
    )


@router.post(
    "/admin/distributions/knowledge",
    response_model=DistributionRead,
    status_code=status.HTTP_201_CREATED,
)
async def create_knowledge_distribution(
    payload: DistributionCreate, session: AsyncSession = Depends(get_db_session)
):
    try:
        record = await info_crawl_service.create_knowledge_distribution(
            session,
            document_version_id=payload.document_version_id,
            target_dataset=payload.target_dataset,
        )
        if payload.dispatch:
            producer = get_celery_producer()
            if producer.enabled:
                producer.dispatch_distribution(record.id)
            else:
                record = await info_crawl_service.dispatch_distribution(
                    session, distribution_id=record.id
                )
        return record
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/admin/distributions", response_model=list[DistributionRead])
async def list_distributions(
    document_version_id: uuid.UUID | None = Query(default=None),
    target_app: str | None = Query(default=None),
    distribution_status: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_db_session),
):
    return await info_crawl_service.list_distributions(
        session,
        document_version_id=document_version_id,
        target_app=target_app,
        status=distribution_status,
        limit=limit,
        offset=offset,
    )


@router.get("/admin/distributions/{distribution_id}", response_model=DistributionRead)
async def get_distribution(
    distribution_id: uuid.UUID, session: AsyncSession = Depends(get_db_session)
):
    record = await info_crawl_service.get_distribution(session, distribution_id)
    if record is None:
        raise HTTPException(status_code=404, detail="distribution not found")
    return record


@router.post("/admin/distributions/{distribution_id}/status", response_model=DistributionRead)
async def update_distribution_status(
    distribution_id: uuid.UUID,
    payload: DistributionStatusUpdate,
    session: AsyncSession = Depends(get_db_session),
):
    try:
        return await info_crawl_service.update_distribution_status(
            session,
            distribution_id=distribution_id,
            status=payload.status,
            last_error=payload.last_error,
            metadata=payload.metadata,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/admin/distributions/{distribution_id}/retry", response_model=DistributionRead)
async def retry_distribution(
    distribution_id: uuid.UUID, session: AsyncSession = Depends(get_db_session)
):
    try:
        return await info_crawl_service.retry_distribution(
            session, distribution_id=distribution_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/admin/distributions/{distribution_id}/dispatch", response_model=DistributionRead)
async def dispatch_distribution(
    distribution_id: uuid.UUID, session: AsyncSession = Depends(get_db_session)
):
    try:
        producer = get_celery_producer()
        if producer.enabled:
            producer.dispatch_distribution(distribution_id)
            record = await info_crawl_service.get_distribution(session, distribution_id)
            if record is None:
                raise ValueError(f"distribution not found: {distribution_id}")
            return record
        return await info_crawl_service.dispatch_distribution(
            session, distribution_id=distribution_id
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/admin/search-index/rebuild", response_model=SearchIndexRebuildRead)
async def rebuild_search_index(
    limit: int = Query(default=200, ge=1, le=1000),
    session: AsyncSession = Depends(get_db_session),
):
    return await info_crawl_service.rebuild_search_index(session, limit=limit)


@router.post(
    "/admin/uploads",
    response_model=UploadIngestRead,
    status_code=status.HTTP_201_CREATED,
)
async def upload_document(
    file: UploadFile = File(...),
    source_id: uuid.UUID | None = Form(default=None),
    title: str | None = Form(default=None),
    session: AsyncSession = Depends(get_db_session),
):
    content = await file.read()
    version = await info_crawl_service.ingest_uploaded_file(
        session,
        filename=file.filename or "upload.bin",
        content=content,
        content_type=file.content_type or "application/octet-stream",
        source_id=source_id,
        title=title,
    )
    return UploadIngestRead(
        document_version_id=version.id,
        document_id=version.document_id,
        extraction_status=version.extraction_status,
        raw_artifact_id=version.raw_artifact_id,
        clean_artifact_id=version.clean_artifact_id,
        text_artifact_id=version.text_artifact_id,
    )
