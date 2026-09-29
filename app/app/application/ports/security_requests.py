"""采集申请要外界做的事（0008-info-intake）。

实现在 `app/infrastructure/securities/`，在 `app/bootstrap/security_requests.py` 里接上。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from app.domain.securities import SecurityCode
from app.domain.securities.requests import (
    DatasetState,
    IngestionState,
    Origin,
    Progress,
    RequestKind,
    RequestStatus,
    SecurityRequest,
)


class OpenRequestExists(Exception):
    """同一家公司已经有一个进行中的申请（别人刚刚抢先建了）。"""


@dataclass(frozen=True)
class NewRequester:
    actor_id: str
    reason: str | None
    origin: Origin | None
    at: datetime


@dataclass(frozen=True)
class WatchlistEntry:
    security_code: str
    added_at: datetime
    added_by: str
    note: str | None
    removed_at: datetime | None = None
    removed_by: str | None = None
    removal_note: str | None = None


class RequestStore(Protocol):
    """一个工作单元里的申请账。写完由调用方 `commit()`；不提交就什么都没发生。"""

    async def lock_requester(self, actor_id: str) -> None:
        """同一个人的几次提交排队进行，免得同时提交绕过「最多几个」的上限。"""
        ...

    async def get(
        self, request_id: str, *, for_update: bool = False
    ) -> SecurityRequest | None: ...

    async def find_open(
        self, code: SecurityCode, *, for_update: bool = False
    ) -> SecurityRequest | None: ...

    async def create(
        self, code: SecurityCode, *, kind: RequestKind, requester: NewRequester
    ) -> SecurityRequest:
        """新建申请。已有进行中的申请时抛 `OpenRequestExists`。"""
        ...

    async def join(self, request_id: str, requester: NewRequester) -> None:
        """加入已有的申请；撤回过的人再加入，算重新加入。"""
        ...

    async def withdraw_requester(
        self, request_id: str, actor_id: str, *, at: datetime
    ) -> None: ...

    async def decide(
        self,
        request_id: str,
        *,
        status: RequestStatus,
        by: str,
        at: datetime,
        note: str | None = None,
        ingestion_id: str | None = None,
    ) -> None: ...

    async def close(self, request_id: str, *, outcome: Progress, at: datetime) -> None:
        """到了终点：记下结果，之后不再变。"""
        ...

    async def open_of(self, actor_id: str) -> list[SecurityRequest]:
        """这个人还在其中、还没有到终点的申请。"""
        ...

    async def of(self, actor_id: str, *, limit: int) -> list[SecurityRequest]: ...

    async def all(
        self, *, status: RequestStatus | None, open_only: bool, limit: int
    ) -> list[SecurityRequest]: ...

    async def ingestion_state(self, ingestion_id: str) -> IngestionState | None: ...

    async def dataset_of(self, ingestion_id: str) -> DatasetState | None: ...

    async def latest_published(self, code: SecurityCode) -> DatasetState | None: ...

    async def start_ingestion(self, code: SecurityCode) -> str:
        """登记一个采集批次并排队，和申请的改动在同一次提交里。返回批次的标识。"""
        ...

    async def commit(self) -> None: ...


class Watchlist(Protocol):
    """关注清单：哪些公司在定时采集的范围里。和申请用同一个工作单元。"""

    async def contains(self, code: SecurityCode) -> bool: ...

    async def entries(
        self, *, include_removed: bool = False
    ) -> list[WatchlistEntry]: ...

    async def add(
        self, code: SecurityCode, *, by: str, note: str | None, at: datetime
    ) -> bool:
        """已在清单里返回 False。"""
        ...

    async def remove(
        self, code: SecurityCode, *, by: str, note: str | None, at: datetime
    ) -> bool:
        """不在清单里返回 False。"""
        ...
