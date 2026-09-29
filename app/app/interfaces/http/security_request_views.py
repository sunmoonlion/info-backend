"""把申请服务的结果排成接口的形状。用户面与管理面共用；链接只从配置里来。"""

from __future__ import annotations

import uuid

from fastapi import HTTPException

from app.application.securities.request_service import Availability, RequestView
from app.domain.cross_app import Origin, return_url
from app.domain.securities.requests import (
    DatasetState,
    Progress,
    RequestError,
)
from app.interfaces.schemas.cross_app import OriginRead
from app.interfaces.schemas.security_requests import (
    AvailabilityRead,
    DatasetRead,
    MineRead,
    RequesterAdminRead,
    SecurityRequestAdminRead,
    SecurityRequestRead,
)
from core.config import Settings

_STATUS = {
    "security_code_invalid": 422,
    "reason_too_long": 422,
    "reason_invalid": 422,
    "rejection_note_required": 422,
    "rejection_note_too_long": 422,
    "request_not_found": 404,
    "too_many_open_requests": 409,
    "request_not_withdrawable": 409,
    "request_not_pending": 409,
    "request_conflict": 409,
}


def problem(error: RequestError) -> HTTPException:
    return HTTPException(
        status_code=_STATUS.get(error.code, 400),
        detail={"code": error.code, "message": error.message},
    )


def origin_read(origin: Origin | None, settings: Settings) -> OriginRead | None:
    if origin is None:
        return None
    source = settings.cross_app_sources().get(origin.app)
    if source is None:  # 配置里已经没有这个应用了：当作没有来处
        return None
    return OriginRead(
        app=origin.app, ref=origin.ref, return_url=return_url(source, origin)
    )


def dataset_read(dataset: DatasetState | None) -> DatasetRead | None:
    """去 knowledge 看这个数据集的链接由网页端拼（只在一处拼），这里只给数据集的编号。"""
    if dataset is None:
        return None
    return DatasetRead(
        dataset_id=dataset.dataset_id,
        data_version=dataset.data_version,
        start_date=dataset.start_date,
        end_date=dataset.end_date,
    )


def user_read(
    view: RequestView, *, actor_id: str, settings: Settings
) -> SecurityRequestRead:
    request = view.request
    me = request.requester(actor_id)
    return SecurityRequestRead(
        id=uuid.UUID(request.request_id),
        security_code=request.security_code,
        kind=request.kind.value,
        progress=view.progress.value,
        created_at=request.created_at,
        closed_at=request.closed_at,
        requesters=request.active_requesters,
        mine=(
            MineRead(
                reason=me.reason,
                origin=origin_read(me.origin, settings),
                requested_at=me.requested_at,
                withdrawn=not me.active,
            )
            if me
            else None
        ),
        can_withdraw=request.can_withdraw(actor_id),
        rejection_note=(
            request.decision_note if view.progress is Progress.REJECTED else None
        ),
        dataset=dataset_read(view.dataset),
    )


def availability_read(
    found: Availability, *, actor_id: str, settings: Settings
) -> AvailabilityRead:
    return AvailabilityRead(
        security_code=found.security_code,
        market=found.market,
        in_watchlist=found.in_watchlist,
        dataset=dataset_read(found.dataset),
        open_request=(
            user_read(found.open_request, actor_id=actor_id, settings=settings)
            if found.open_request
            else None
        ),
        mine=found.mine,
        my_open_count=found.my_open_count,
        max_open=found.max_open,
        needs_approval=not found.in_watchlist,
    )


def admin_read(
    view: RequestView, *, watched: frozenset[str], settings: Settings
) -> SecurityRequestAdminRead:
    request = view.request
    return SecurityRequestAdminRead(
        id=uuid.UUID(request.request_id),
        security_code=request.security_code,
        kind=request.kind.value,
        status=request.status.value,
        progress=view.progress.value,
        open=request.open,
        created_at=request.created_at,
        closed_at=request.closed_at,
        decided_by=request.decided_by,
        decided_at=request.decided_at,
        decision_note=request.decision_note,
        ingestion_id=(
            uuid.UUID(request.ingestion_id) if request.ingestion_id else None
        ),
        in_watchlist=request.security_code in watched,
        requesters=[
            RequesterAdminRead(
                actor_id=uuid.UUID(one.actor_id),
                reason=one.reason,
                origin=origin_read(one.origin, settings),
                requested_at=one.requested_at,
                withdrawn_at=one.withdrawn_at,
            )
            for one in request.requesters
        ],
        dataset=dataset_read(view.dataset),
    )
