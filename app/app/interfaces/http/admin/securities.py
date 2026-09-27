"""证券采集与数据集的管理接口（0008-info）。挂在管理面下，要求 info 管理员。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bootstrap.securities import (
    build_dataset_registration_service,
    request_security_ingestion,
)
from app.domain.securities import InvalidSecurityCode, SecurityCode
from app.domain.securities.registration import RegistrationError
from app.infrastructure.models.securities import (
    SecurityDataset,
    SecurityIngestion,
    SecurityIngestionItem,
)
from app.infrastructure.storage.postgres import get_db_session, get_postgres
from app.interfaces.schemas.securities import (
    SecurityDatasetRead,
    SecurityIngestionDetail,
    SecurityIngestionItemRead,
    SecurityIngestionRead,
)

router = APIRouter(tags=["证券采集"])


def _code(raw: str) -> SecurityCode:
    try:
        return SecurityCode(raw)
    except InvalidSecurityCode as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None


@router.post(
    "/admin/securities/{code}/ingestions",
    response_model=SecurityIngestionRead,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_security_ingestion(
    code: str, session: AsyncSession = Depends(get_db_session)
):
    """登记一个采集批次并排队执行，立即返回。进度用批次标识查询。"""
    return await request_security_ingestion(
        session, get_postgres().session_factory, _code(code).code
    )


@router.get(
    "/admin/securities/{code}/ingestions", response_model=list[SecurityIngestionRead]
)
async def list_security_ingestions(
    code: str,
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
):
    rows = await session.execute(
        select(SecurityIngestion)
        .where(SecurityIngestion.security_code == _code(code).code)
        .order_by(SecurityIngestion.requested_at.desc())
        .limit(limit)
    )
    return list(rows.scalars())


@router.get(
    "/admin/security-ingestions/{ingestion_id}", response_model=SecurityIngestionDetail
)
async def get_security_ingestion(
    ingestion_id: uuid.UUID, session: AsyncSession = Depends(get_db_session)
):
    batch = await session.get(SecurityIngestion, ingestion_id)
    if batch is None:
        raise HTTPException(status_code=404, detail="security ingestion not found")
    items = await session.execute(
        select(SecurityIngestionItem)
        .where(SecurityIngestionItem.ingestion_id == ingestion_id)
        .order_by(SecurityIngestionItem.seq)
    )
    return SecurityIngestionDetail(
        **SecurityIngestionRead.model_validate(batch).model_dump(),
        items=[SecurityIngestionItemRead.model_validate(i) for i in items.scalars()],
    )


def _dataset(row: SecurityDataset) -> SecurityDatasetRead:
    return SecurityDatasetRead(
        id=row.id,
        security_code=row.security_code,
        dataset_id=row.dataset_id,
        data_version=row.data_version,
        status=row.status,
        ingestion_id=row.ingestion_id,
        sha256=row.sha256,
        size_bytes=row.size_bytes,
        row_counts=row.row_counts,
        start_date=row.start_date,
        end_date=row.end_date,
        built_at=row.built_at,
        failed_checks=list((row.quality or {}).get("failed_blocking") or []),
        knowledge_registered_at=row.knowledge_registered_at,
        knowledge_registration_error=row.knowledge_registration_error,
    )


@router.get(
    "/admin/securities/{code}/datasets", response_model=list[SecurityDatasetRead]
)
async def list_security_datasets(
    code: str,
    limit: int = Query(default=20, ge=1, le=100),
    session: AsyncSession = Depends(get_db_session),
):
    """该代码建出过的数据集，新的在前；含质量检查没过的与登记的结果。"""
    rows = await session.execute(
        select(SecurityDataset)
        .where(SecurityDataset.security_code == _code(code).code)
        .order_by(SecurityDataset.built_at.desc())
        .limit(limit)
    )
    return [_dataset(row) for row in rows.scalars()]


@router.post(
    "/admin/security-datasets/{dataset_record_id}/registration",
    response_model=SecurityDatasetRead,
)
async def register_security_dataset(
    dataset_record_id: uuid.UUID, session: AsyncSession = Depends(get_db_session)
):
    """向知识服务登记这个版本并使它成为现行版本；也用于登记失败后的重试与回退。"""
    service = build_dataset_registration_service(get_postgres().session_factory)
    try:
        await service.register(str(dataset_record_id))
    except RegistrationError as exc:
        if exc.code == "dataset_not_found":
            raise HTTPException(status_code=404, detail=exc.code) from None
        status_code = 503 if exc.retryable else 409
        raise HTTPException(status_code=status_code, detail=exc.code) from None
    row = await session.get(SecurityDataset, dataset_record_id)
    if row is None:
        raise HTTPException(status_code=404, detail="dataset_not_found")
    return _dataset(row)
