"""采集申请与关注清单的管理面（0008-info-intake）。要求 info 管理员。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.bootstrap.security_requests import build_security_request_service
from app.domain.securities.requests import RequestError, RequestStatus
from app.domain.security import Principal
from app.infrastructure.storage.postgres import get_db_session, get_postgres
from app.interfaces.http.middleware.auth import require_info_admin
from app.interfaces.http.security_request_views import admin_read, problem
from app.interfaces.schemas.security_requests import (
    ApproveRequest,
    RejectRequest,
    SecurityRequestAdminRead,
    UnwatchRequest,
    WatchlistEntryRead,
    WatchRequest,
)
from core.config import get_settings

router = APIRouter(prefix="/admin", tags=["采集申请"])


def _actor(principal: Principal) -> str:
    if principal.actor_id is None:
        raise HTTPException(status_code=403, detail="principal has no actor id")
    return str(principal.actor_id)


def _service(session: AsyncSession):
    return build_security_request_service(session, get_postgres().session_factory)


async def _watched(service) -> frozenset[str]:
    return frozenset(entry.security_code for entry in await service.watchlist())


@router.get("/security-requests", response_model=list[SecurityRequestAdminRead])
async def listing(
    status: RequestStatus | None = Query(default=None),
    open_only: bool = Query(default=False, alias="open"),
    limit: int = Query(default=100, ge=1, le=200),
    session: AsyncSession = Depends(get_db_session),
):
    service = _service(session)
    views = await service.listing(status=status, open_only=open_only, limit=limit)
    watched, settings = await _watched(service), get_settings()
    return [admin_read(view, watched=watched, settings=settings) for view in views]


@router.post(
    "/security-requests/{request_id}/approval",
    response_model=SecurityRequestAdminRead,
)
async def approve(
    request_id: str,
    body: ApproveRequest | None = None,
    principal: Principal = Depends(require_info_admin),
    session: AsyncSession = Depends(get_db_session),
):
    """批准：登记采集批次并排队，和申请的状态一起提交。"""
    service = _service(session)
    try:
        view = await service.approve(
            request_id,
            by=_actor(principal),
            add_to_watchlist=bool(body and body.add_to_watchlist),
        )
    except RequestError as exc:
        raise problem(exc) from None
    return admin_read(view, watched=await _watched(service), settings=get_settings())


@router.post(
    "/security-requests/{request_id}/rejection",
    response_model=SecurityRequestAdminRead,
)
async def reject(
    request_id: str,
    body: RejectRequest,
    principal: Principal = Depends(require_info_admin),
    session: AsyncSession = Depends(get_db_session),
):
    service = _service(session)
    try:
        view = await service.reject(request_id, by=_actor(principal), note=body.note)
    except RequestError as exc:
        raise problem(exc) from None
    return admin_read(view, watched=await _watched(service), settings=get_settings())


@router.get("/security-watchlist", response_model=list[WatchlistEntryRead])
async def watchlist(
    include_removed: bool = Query(default=False),
    session: AsyncSession = Depends(get_db_session),
):
    entries = await _service(session).watchlist(include_removed=include_removed)
    return [WatchlistEntryRead(**vars(entry)) for entry in entries]


@router.post("/security-watchlist", response_model=list[WatchlistEntryRead])
async def watch(
    body: WatchRequest,
    principal: Principal = Depends(require_info_admin),
    session: AsyncSession = Depends(get_db_session),
):
    service = _service(session)
    try:
        await service.watch(body.security_code, by=_actor(principal), note=body.note)
    except RequestError as exc:
        raise problem(exc) from None
    return [WatchlistEntryRead(**vars(entry)) for entry in await service.watchlist()]


@router.post(
    "/security-watchlist/{code}/removal", response_model=list[WatchlistEntryRead]
)
async def unwatch(
    code: str,
    body: UnwatchRequest | None = None,
    principal: Principal = Depends(require_info_admin),
    session: AsyncSession = Depends(get_db_session),
):
    service = _service(session)
    try:
        removed = await service.unwatch(
            code, by=_actor(principal), note=body.note if body else None
        )
    except RequestError as exc:
        raise problem(exc) from None
    if not removed:
        raise HTTPException(
            status_code=404,
            detail={"code": "not_on_watchlist", "message": "这家公司不在关注清单里"},
        )
    return [WatchlistEntryRead(**vars(entry)) for entry in await service.watchlist()]
