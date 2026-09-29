"""采集申请（0008-info-intake）：用户提出，所有者批准，批准之后走现有的排队流程。

规则都在这里与领域层；页面、接口、联调脚本调的是同一个服务。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime

from app.application.ports.security_requests import (
    NewRequester,
    OpenRequestExists,
    RequestStore,
    Watchlist,
    WatchlistEntry,
)
from app.domain.cross_app import clean_origin
from app.domain.securities import InvalidSecurityCode, SecurityCode
from app.domain.securities.requests import (
    DEFAULT_MAX_OPEN_REQUESTS,
    FINAL,
    MAX_REASON_CHARS,
    SYSTEM_WATCHLIST,
    DatasetState,
    Progress,
    RequestError,
    RequestKind,
    RequestStatus,
    SecurityRequest,
    clean_reason,
    progress_after_approval,
)


@dataclass(frozen=True)
class RequestView:
    """一个申请连同它现在的进度。给谁看、露哪些，由接口层决定。"""

    request: SecurityRequest
    progress: Progress
    dataset: DatasetState | None


@dataclass(frozen=True)
class Availability:
    security_code: str
    market: str
    in_watchlist: bool
    dataset: DatasetState | None  # 现有的、已发布的数据
    open_request: RequestView | None
    mine: bool  # 我是不是进行中那个申请的申请人
    my_open_count: int
    max_open: int


class SecurityRequestService:
    def __init__(
        self,
        *,
        store: RequestStore,
        watchlist: Watchlist,
        clock: Callable[[], datetime],
        known_apps: frozenset[str] = frozenset(),
        max_open: int = DEFAULT_MAX_OPEN_REQUESTS,
        registration_enabled: bool = True,
    ) -> None:
        if max_open < 1:
            raise ValueError("max_open must be at least 1")
        self._store = store
        self._watchlist = watchlist
        self._clock = clock
        self._known_apps = known_apps
        self._max_open = max_open
        self._registration_enabled = registration_enabled

    # ------------------------------------------------------------ 进度
    async def _view(self, request: SecurityRequest) -> RequestView:
        """算出进度；刚到终点的，顺手记回申请上。"""
        if not request.open or request.outcome is not None:
            outcome = request.outcome or _closed_progress(request.status)
            dataset = (
                await self._store.dataset_of(request.ingestion_id)
                if request.ingestion_id and outcome is Progress.AVAILABLE
                else None
            )
            return RequestView(request, outcome, dataset)
        if request.status is RequestStatus.PENDING:
            return RequestView(request, Progress.PENDING, None)
        ingestion = (
            await self._store.ingestion_state(request.ingestion_id)
            if request.ingestion_id
            else None
        )
        dataset = (
            await self._store.dataset_of(request.ingestion_id)
            if request.ingestion_id
            else None
        )
        progress = progress_after_approval(
            ingestion, dataset, registration_enabled=self._registration_enabled
        )
        if progress in FINAL:
            await self._store.close(
                request.request_id, outcome=progress, at=self._clock()
            )
        return RequestView(
            request, progress, dataset if progress is Progress.AVAILABLE else None
        )

    async def _views(self, requests: list[SecurityRequest]) -> list[RequestView]:
        return [await self._view(one) for one in requests]

    async def _my_open_count(self, actor_id: str) -> int:
        views = await self._views(await self._store.open_of(actor_id))
        return sum(1 for view in views if view.progress not in FINAL)

    # ------------------------------------------------------------ 用户
    async def availability(self, raw_code: str, *, actor_id: str) -> Availability:
        code = _code(raw_code)
        current = await self._store.find_open(code)
        view = await self._view(current) if current else None
        if view is not None and view.progress in FINAL:
            view = None  # 刚好到了终点：不算进行中
        result = Availability(
            security_code=code.code,
            market=str(code.market),
            in_watchlist=await self._watchlist.contains(code),
            dataset=await self._store.latest_published(code),
            open_request=view,
            mine=bool(
                view
                and (me := view.request.requester(actor_id)) is not None
                and me.active
            ),
            my_open_count=await self._my_open_count(actor_id),
            max_open=self._max_open,
        )
        await self._store.commit()
        return result

    async def submit(
        self,
        raw_code: str,
        *,
        actor_id: str,
        reason: str | None = None,
        origin_app: str | None = None,
        origin_ref: str | None = None,
    ) -> RequestView:
        code = _code(raw_code)
        requester = NewRequester(
            actor_id=actor_id,
            reason=clean_reason(reason),
            origin=clean_origin(origin_app, origin_ref, known_apps=self._known_apps),
            at=self._clock(),
        )
        await self._store.lock_requester(actor_id)
        current = await self._open_for(code)
        if current is not None:
            mine = current.requester(actor_id)
            if mine is not None and mine.active:
                view = await self._view(current)  # 重复提交：原样返回
                await self._store.commit()
                return view
        await self._ensure_room(actor_id)
        if current is None:
            try:
                current = await self._create(code, requester)
            except OpenRequestExists:
                current = await self._open_for(code)
                if current is None:
                    raise RequestError(
                        "request_conflict", "刚才有人同时提交了申请，请再试一次"
                    ) from None
                await self._store.join(current.request_id, requester)
        else:
            await self._store.join(current.request_id, requester)
        fresh = await self._store.get(current.request_id)
        assert fresh is not None
        view = await self._view(fresh)
        await self._store.commit()
        return view

    async def _open_for(self, code: SecurityCode) -> SecurityRequest | None:
        current = await self._store.find_open(code, for_update=True)
        if current is None:
            return None
        view = await self._view(current)
        return None if view.progress in FINAL else current

    async def _ensure_room(self, actor_id: str) -> None:
        if await self._my_open_count(actor_id) >= self._max_open:
            raise RequestError(
                "too_many_open_requests",
                f"你还有 {self._max_open} 个申请没有完成，等它们完成或撤回之后再提",
            )

    async def _create(
        self, code: SecurityCode, requester: NewRequester
    ) -> SecurityRequest:
        existing = await self._store.latest_published(code)
        created = await self._store.create(
            code,
            kind=RequestKind.REFRESH if existing else RequestKind.INITIAL,
            requester=requester,
        )
        if await self._watchlist.contains(code):
            await self._approve(created, by=SYSTEM_WATCHLIST)
        return created

    async def withdraw(self, request_id: str, *, actor_id: str) -> RequestView:
        request = await self._store.get(request_id, for_update=True)
        if request is None or request.requester(actor_id) is None:
            raise RequestError("request_not_found", "没有这个申请")
        if not request.can_withdraw(actor_id):
            raise RequestError(
                "request_not_withdrawable", "这个申请已经批准或已经结束，不能撤回"
            )
        now = self._clock()
        await self._store.withdraw_requester(request_id, actor_id, at=now)
        if request.active_requesters <= 1:  # 撤回的是最后一个人
            await self._store.decide(
                request_id, status=RequestStatus.WITHDRAWN, by=actor_id, at=now
            )
            await self._store.close(request_id, outcome=Progress.WITHDRAWN, at=now)
        fresh = await self._store.get(request_id)
        assert fresh is not None
        view = await self._view(fresh)
        await self._store.commit()
        return view

    async def mine(self, *, actor_id: str, limit: int = 50) -> list[RequestView]:
        views = await self._views(await self._store.of(actor_id, limit=limit))
        await self._store.commit()
        return views

    async def one_of_mine(self, request_id: str, *, actor_id: str) -> RequestView:
        request = await self._store.get(request_id)
        if request is None or request.requester(actor_id) is None:
            raise RequestError("request_not_found", "没有这个申请")
        view = await self._view(request)
        await self._store.commit()
        return view

    # ------------------------------------------------------------ 所有者
    async def listing(
        self,
        *,
        status: RequestStatus | None = None,
        open_only: bool = False,
        limit: int = 100,
    ) -> list[RequestView]:
        views = await self._views(
            await self._store.all(status=status, open_only=open_only, limit=limit)
        )
        await self._store.commit()
        return views

    async def approve(
        self, request_id: str, *, by: str, add_to_watchlist: bool = False
    ) -> RequestView:
        request = await self._pending(request_id)
        await self._approve(request, by=by)
        if add_to_watchlist:
            await self._watchlist.add(
                SecurityCode(request.security_code),
                by=by,
                note="批准申请时加入",
                at=self._clock(),
            )
        return await self._after_decision(request_id)

    async def reject(self, request_id: str, *, by: str, note: str) -> RequestView:
        reason = (note or "").strip()
        if not reason:
            raise RequestError(
                "rejection_note_required", "拒绝要写一句原因，申请人看得到"
            )
        if len(reason) > MAX_REASON_CHARS:
            raise RequestError(
                "rejection_note_too_long", f"原因最多 {MAX_REASON_CHARS} 个字"
            )
        request = await self._pending(request_id)
        now = self._clock()
        await self._store.decide(
            request.request_id,
            status=RequestStatus.REJECTED,
            by=by,
            at=now,
            note=reason,
        )
        await self._store.close(request.request_id, outcome=Progress.REJECTED, at=now)
        return await self._after_decision(request_id)

    async def _pending(self, request_id: str) -> SecurityRequest:
        request = await self._store.get(request_id, for_update=True)
        if request is None:
            raise RequestError("request_not_found", "没有这个申请")
        if not request.open or request.status is not RequestStatus.PENDING:
            raise RequestError("request_not_pending", "这个申请已经处理过了")
        return request

    async def _approve(self, request: SecurityRequest, *, by: str) -> None:
        ingestion_id = await self._store.start_ingestion(
            SecurityCode(request.security_code)
        )
        await self._store.decide(
            request.request_id,
            status=RequestStatus.APPROVED,
            by=by,
            at=self._clock(),
            ingestion_id=ingestion_id,
        )

    async def _after_decision(self, request_id: str) -> RequestView:
        fresh = await self._store.get(request_id)
        assert fresh is not None
        view = await self._view(fresh)
        await self._store.commit()
        return view

    # ------------------------------------------------------------ 关注清单
    async def watchlist(self, *, include_removed: bool = False) -> list[WatchlistEntry]:
        return await self._watchlist.entries(include_removed=include_removed)

    async def watch(self, raw_code: str, *, by: str, note: str | None) -> bool:
        added = await self._watchlist.add(
            _code(raw_code), by=by, note=clean_reason(note), at=self._clock()
        )
        await self._store.commit()
        return added

    async def unwatch(self, raw_code: str, *, by: str, note: str | None) -> bool:
        removed = await self._watchlist.remove(
            _code(raw_code), by=by, note=clean_reason(note), at=self._clock()
        )
        await self._store.commit()
        return removed


def _code(raw: str) -> SecurityCode:
    try:
        return SecurityCode(raw)
    except InvalidSecurityCode as exc:
        raise RequestError("security_code_invalid", str(exc)) from None


def _closed_progress(status: RequestStatus) -> Progress:
    if status is RequestStatus.REJECTED:
        return Progress.REJECTED
    if status is RequestStatus.WITHDRAWN:
        return Progress.WITHDRAWN
    return Progress.FAILED  # 关了却没有记结果：不该发生，按失败显示
