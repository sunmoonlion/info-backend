"""采集申请的用户面（0008-info-intake）。登录的用户；只看得到、动得了自己的申请。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.bootstrap.security_requests import build_security_request_service
from app.domain.securities.requests import RequestError, clean_origin
from app.domain.security import Principal
from app.infrastructure.storage.postgres import get_db_session, get_postgres
from app.interfaces.http.middleware.auth import get_web_current_user
from app.interfaces.http.security_request_views import (
    availability_read,
    origin_read,
    problem,
    user_read,
)
from app.interfaces.schemas.security_requests import (
    AvailabilityRead,
    OriginRead,
    SecurityRequestRead,
    SubmitRequest,
)
from core.config import get_settings

router = APIRouter(prefix="/web/v1", tags=["采集申请"])


def _actor(principal: Principal) -> str:
    if principal.actor_id is None:
        raise HTTPException(status_code=403, detail="principal has no actor id")
    return str(principal.actor_id)


def _service(session: AsyncSession):
    return build_security_request_service(session, get_postgres().session_factory)


@router.get("/security-request-origin", response_model=OriginRead | None)
async def resolve_origin(
    source: str | None = Query(default=None, alias="from", max_length=64),
    ref: str | None = Query(default=None, max_length=256),
    _: Principal = Depends(get_web_current_user),
):
    """链接带来的「从哪来」：认得就返回它和回跳地址，认不得返回空。"""
    settings = get_settings()
    origin = clean_origin(
        source, ref, known_apps=frozenset(settings.security_request_sources())
    )
    return origin_read(origin, settings)


@router.get("/securities/{code}/availability", response_model=AvailabilityRead)
async def availability(
    code: str,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    actor = _actor(principal)
    try:
        found = await _service(session).availability(code, actor_id=actor)
    except RequestError as exc:
        raise problem(exc) from None
    return availability_read(found, actor_id=actor, settings=get_settings())


@router.post(
    "/security-requests",
    response_model=SecurityRequestRead,
    status_code=status.HTTP_201_CREATED,
)
async def submit(
    body: SubmitRequest,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    """提出申请，或加入已有的申请。重复提交返回原来那一个。"""
    actor = _actor(principal)
    try:
        view = await _service(session).submit(
            body.security_code,
            actor_id=actor,
            reason=body.reason,
            origin_app=body.source,
            origin_ref=body.ref,
        )
    except RequestError as exc:
        raise problem(exc) from None
    return user_read(view, actor_id=actor, settings=get_settings())


@router.get("/security-requests", response_model=list[SecurityRequestRead])
async def mine(
    limit: int = Query(default=50, ge=1, le=100),
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    actor = _actor(principal)
    views = await _service(session).mine(actor_id=actor, limit=limit)
    settings = get_settings()
    return [user_read(view, actor_id=actor, settings=settings) for view in views]


@router.get("/security-requests/{request_id}", response_model=SecurityRequestRead)
async def one(
    request_id: str,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    actor = _actor(principal)
    try:
        view = await _service(session).one_of_mine(request_id, actor_id=actor)
    except RequestError as exc:
        raise problem(exc) from None
    return user_read(view, actor_id=actor, settings=get_settings())


@router.post(
    "/security-requests/{request_id}/withdrawal", response_model=SecurityRequestRead
)
async def withdraw(
    request_id: str,
    principal: Principal = Depends(get_web_current_user),
    session: AsyncSession = Depends(get_db_session),
):
    actor = _actor(principal)
    try:
        view = await _service(session).withdraw(request_id, actor_id=actor)
    except RequestError as exc:
        raise problem(exc) from None
    return user_read(view, actor_id=actor, settings=get_settings())
