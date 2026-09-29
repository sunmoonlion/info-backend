"""采集申请测试用的内存实现：假的账本与关注清单。"""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta

from app.application.ports.security_requests import (
    NewRequester,
    OpenRequestExists,
    WatchlistEntry,
)
from app.application.securities.request_service import SecurityRequestService
from app.domain.securities import SecurityCode
from app.domain.securities.requests import (
    DatasetState,
    IngestionState,
    Progress,
    Requester,
    RequestStatus,
    SecurityRequest,
)

ALICE = "00000000-0000-4000-8000-00000000000a"
BOB = "00000000-0000-4000-8000-00000000000b"
OWNER = "00000000-0000-4000-8000-0000000000ff"
START = datetime(2026, 9, 29, 9, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = START

    def __call__(self) -> datetime:
        self.now += timedelta(seconds=1)
        return self.now


class MemoryStore:
    def __init__(self) -> None:
        self.requests: dict[str, SecurityRequest] = {}
        self.ingestions: dict[str, IngestionState] = {}
        self.datasets: dict[str, DatasetState] = {}  # 按批次
        self.published: dict[str, DatasetState] = {}  # 按代码
        self.started: list[str] = []
        self.commits = 0
        self.locked: list[str] = []
        self.race: SecurityRequest | None = None  # 模拟别人抢先建了申请

    async def lock_requester(self, actor_id: str) -> None:
        self.locked.append(actor_id)

    async def get(self, request_id, *, for_update=False):
        return self.requests.get(request_id)

    async def find_open(self, code, *, for_update=False):
        for one in self.requests.values():
            if one.security_code == code.code and one.open:
                return one
        return None

    async def create(self, code, *, kind, requester: NewRequester):
        if self.race is not None:
            self.requests[self.race.request_id], self.race = self.race, None
            raise OpenRequestExists
        if await self.find_open(code):
            raise OpenRequestExists
        created = SecurityRequest(
            request_id=str(uuid.uuid4()),
            security_code=code.code,
            kind=kind,
            status=RequestStatus.PENDING,
            open=True,
            created_at=requester.at,
            requesters=(_requester(requester),),
        )
        self.requests[created.request_id] = created
        return created

    async def join(self, request_id, requester: NewRequester) -> None:
        one = self.requests[request_id]
        others = tuple(r for r in one.requesters if r.actor_id != requester.actor_id)
        self.requests[request_id] = replace(
            one, requesters=(*others, _requester(requester))
        )

    async def withdraw_requester(self, request_id, actor_id, *, at) -> None:
        one = self.requests[request_id]
        self.requests[request_id] = replace(
            one,
            requesters=tuple(
                replace(r, withdrawn_at=at) if r.actor_id == actor_id else r
                for r in one.requesters
            ),
        )

    async def decide(
        self, request_id, *, status, by, at, note=None, ingestion_id=None
    ) -> None:
        self.requests[request_id] = replace(
            self.requests[request_id],
            status=status,
            decided_by=by,
            decided_at=at,
            decision_note=note,
            ingestion_id=ingestion_id,
        )

    async def close(self, request_id, *, outcome: Progress, at) -> None:
        self.requests[request_id] = replace(
            self.requests[request_id], open=False, outcome=outcome, closed_at=at
        )

    async def open_of(self, actor_id):
        return [
            one
            for one in self.requests.values()
            if one.open and (me := one.requester(actor_id)) and me.active
        ]

    async def of(self, actor_id, *, limit):
        mine = [one for one in self.requests.values() if one.requester(actor_id)]
        return sorted(mine, key=lambda r: r.created_at, reverse=True)[:limit]

    async def all(self, *, status, open_only, limit):
        rows = [
            one
            for one in self.requests.values()
            if (status is None or one.status is status) and (not open_only or one.open)
        ]
        return sorted(rows, key=lambda r: r.created_at)[:limit]

    async def ingestion_state(self, ingestion_id):
        return self.ingestions.get(ingestion_id)

    async def dataset_of(self, ingestion_id):
        return self.datasets.get(ingestion_id)

    async def latest_published(self, code):
        return self.published.get(code.code)

    async def start_ingestion(self, code) -> str:
        ingestion_id = str(uuid.uuid4())
        self.ingestions[ingestion_id] = IngestionState(status="pending")
        self.started.append(code.code)
        return ingestion_id

    async def commit(self) -> None:
        self.commits += 1


class MemoryWatchlist:
    def __init__(self, *codes: str) -> None:
        self.rows: dict[str, WatchlistEntry] = {
            code: WatchlistEntry(code, START, "system:seed", None) for code in codes
        }

    async def contains(self, code: SecurityCode) -> bool:
        row = self.rows.get(code.code)
        return row is not None and row.removed_at is None

    async def entries(self, *, include_removed=False):
        return [
            row
            for row in self.rows.values()
            if include_removed or row.removed_at is None
        ]

    async def add(self, code, *, by, note, at) -> bool:
        if await self.contains(code):
            return False
        self.rows[code.code] = WatchlistEntry(code.code, at, by, note)
        return True

    async def remove(self, code, *, by, note, at) -> bool:
        if not await self.contains(code):
            return False
        self.rows[code.code] = replace(
            self.rows[code.code], removed_at=at, removed_by=by, removal_note=note
        )
        return True


def _requester(new: NewRequester) -> Requester:
    return Requester(
        actor_id=new.actor_id,
        reason=new.reason,
        origin=new.origin,
        requested_at=new.at,
    )


def dataset(version: str = "sh600519-financials-aaaa", *, registered=True, **changes):
    values = {
        "status": "published",
        "dataset_id": version.rsplit("-", 1)[0],
        "data_version": version,
        "start_date": "2016-12-31",
        "end_date": "2025-12-31",
        "registered": registered,
    }
    return DatasetState(**(values | changes))


def build(*watched: str, **options):
    store, watchlist = MemoryStore(), MemoryWatchlist(*watched)
    service = SecurityRequestService(
        store=store,
        watchlist=watchlist,
        clock=Clock(),
        known_apps=frozenset({"investment", "knowledge"}),
        **options,
    )
    return service, store, watchlist
