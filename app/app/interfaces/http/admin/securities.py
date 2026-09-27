"""证券采集的管理接口（0008-info 段一）。挂在管理面下，要求 info 管理员。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.bootstrap.securities import request_security_ingestion
from app.domain.securities import InvalidSecurityCode, SecurityCode
from app.infrastructure.models.securities import (
    SecurityIngestion,
    SecurityIngestionItem,
)
from app.infrastructure.storage.postgres import get_db_session, get_postgres
from app.interfaces.schemas.securities import (
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
