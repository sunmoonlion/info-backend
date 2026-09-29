"""采集申请与关注清单在 PostgreSQL 里的实现（0008-info-intake）。

两者用同一个数据库会话：申请的改动、关注清单的改动、登记采集批次、排队，一次提交。
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.security_requests import (
    NewRequester,
    OpenRequestExists,
    WatchlistEntry,
)
from app.domain.securities import SecurityCode
from app.domain.securities.requests import (
    DatasetState,
    IngestionState,
    Origin,
    Progress,
    Requester,
    RequestKind,
    RequestStatus,
    SecurityRequest,
)
from app.infrastructure.models.securities import (
    SecurityDataset,
    SecurityIngestion,
    SecurityRequestRequester,
    SecurityWatchlist,
    SecurityWatchlistLog,
)
from app.infrastructure.models.securities import (
    SecurityRequest as RequestRow,
)

StartIngestion = Callable[[AsyncSession, SecurityCode], Awaitable[str]]
_OPEN_INDEX = "uq_security_request_open_code"


def _uuid(value: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _dataset(row: SecurityDataset) -> DatasetState:
    return DatasetState(
        status=row.status,
        dataset_id=row.dataset_id,
        data_version=row.data_version,
        start_date=row.start_date,
        end_date=row.end_date,
        registered=row.knowledge_registered_at is not None,
        registration_error=row.knowledge_registration_error,
    )


class SqlRequestStore:
    def __init__(self, session: AsyncSession, *, start_ingestion: StartIngestion):
        self._session = session
        self._start = start_ingestion

    async def lock_requester(self, actor_id: str) -> None:
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtext(:key))"),
            {"key": f"security_request:{actor_id}"},
        )

    async def _load(self, row: RequestRow) -> SecurityRequest:
        people = (
            await self._session.execute(
                select(SecurityRequestRequester)
                .where(SecurityRequestRequester.request_id == row.id)
                .order_by(SecurityRequestRequester.requested_at)
            )
        ).scalars()
        return SecurityRequest(
            request_id=str(row.id),
            security_code=row.security_code,
            kind=RequestKind(row.kind),
            status=RequestStatus(row.status),
            open=row.open,
            created_at=row.created_at,
            requesters=tuple(
                Requester(
                    actor_id=str(one.actor_id),
                    reason=one.reason,
                    origin=(
                        Origin(app=one.source_app, ref=one.source_ref)
                        if one.source_app
                        else None
                    ),
                    requested_at=one.requested_at,
                    withdrawn_at=one.withdrawn_at,
                )
                for one in people
            ),
            ingestion_id=str(row.ingestion_id) if row.ingestion_id else None,
            decided_by=row.decided_by,
            decided_at=row.decided_at,
            decision_note=row.decision_note,
            outcome=Progress(row.outcome) if row.outcome else None,
            closed_at=row.closed_at,
        )

    async def _row(self, request_id: str, *, for_update: bool = False):
        key = _uuid(request_id)
        if key is None:
            return None
        query = select(RequestRow).where(RequestRow.id == key)
        if for_update:
            query = query.with_for_update()
        return (await self._session.execute(query)).scalar_one_or_none()

    async def _must(self, request_id: str) -> RequestRow:
        row = await self._row(request_id, for_update=True)
        if row is None:
            raise RuntimeError("security_request_missing")
        return row

    async def get(self, request_id: str, *, for_update: bool = False):
        row = await self._row(request_id, for_update=for_update)
        if row is None:
            return None
        await self._session.refresh(row)
        return await self._load(row)

    async def find_open(self, code: SecurityCode, *, for_update: bool = False):
        query = select(RequestRow).where(
            RequestRow.security_code == code.code, RequestRow.open
        )
        if for_update:
            query = query.with_for_update()
        row = (await self._session.execute(query)).scalar_one_or_none()
        return await self._load(row) if row else None

    async def create(
        self, code: SecurityCode, *, kind: RequestKind, requester: NewRequester
    ) -> SecurityRequest:
        row = RequestRow(
            security_code=code.code,
            kind=kind.value,
            status=RequestStatus.PENDING.value,
            open=True,
        )
        try:
            # 用保存点：撞上唯一索引时只回退这一句，调用方的事务还能接着用
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
        except IntegrityError as exc:
            if _OPEN_INDEX in str(exc.orig):
                raise OpenRequestExists from None
            raise
        await self._add_requester(row.id, requester)
        await self._session.refresh(row)
        return await self._load(row)

    async def _add_requester(self, request_id: uuid.UUID, new: NewRequester) -> None:
        self._session.add(
            SecurityRequestRequester(
                request_id=request_id,
                actor_id=uuid.UUID(new.actor_id),
                reason=new.reason,
                source_app=new.origin.app if new.origin else None,
                source_ref=new.origin.ref if new.origin else None,
                requested_at=new.at,
            )
        )
        await self._session.flush()

    async def join(self, request_id: str, requester: NewRequester) -> None:
        key = uuid.UUID(request_id)
        existing = (
            await self._session.execute(
                select(SecurityRequestRequester)
                .where(
                    SecurityRequestRequester.request_id == key,
                    SecurityRequestRequester.actor_id == uuid.UUID(requester.actor_id),
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if existing is None:
            await self._add_requester(key, requester)
            return
        existing.reason = requester.reason
        existing.source_app = requester.origin.app if requester.origin else None
        existing.source_ref = requester.origin.ref if requester.origin else None
        existing.requested_at = requester.at
        existing.withdrawn_at = None
        await self._session.flush()

    async def withdraw_requester(
        self, request_id: str, actor_id: str, *, at: datetime
    ) -> None:
        row = (
            await self._session.execute(
                select(SecurityRequestRequester)
                .where(
                    SecurityRequestRequester.request_id == uuid.UUID(request_id),
                    SecurityRequestRequester.actor_id == uuid.UUID(actor_id),
                )
                .with_for_update()
            )
        ).scalar_one()
        row.withdrawn_at = at
        await self._session.flush()

    async def decide(
        self,
        request_id: str,
        *,
        status: RequestStatus,
        by: str,
        at: datetime,
        note: str | None = None,
        ingestion_id: str | None = None,
    ) -> None:
        row = await self._must(request_id)
        row.status = status.value
        row.decided_by = by[:64]
        row.decided_at = at
        row.decision_note = note
        if ingestion_id is not None:
            row.ingestion_id = uuid.UUID(ingestion_id)
        await self._session.flush()

    async def close(self, request_id: str, *, outcome: Progress, at: datetime) -> None:
        row = await self._must(request_id)
        if not row.open:
            return  # 已经记过结果：不改
        row.open = False
        row.outcome = outcome.value
        row.closed_at = at
        await self._session.flush()

    async def _many(self, query) -> list[SecurityRequest]:
        rows = (await self._session.execute(query)).scalars().all()
        return [await self._load(row) for row in rows]

    async def open_of(self, actor_id: str) -> list[SecurityRequest]:
        return await self._many(
            select(RequestRow)
            .join(
                SecurityRequestRequester,
                SecurityRequestRequester.request_id == RequestRow.id,
            )
            .where(
                RequestRow.open,
                SecurityRequestRequester.actor_id == uuid.UUID(actor_id),
                SecurityRequestRequester.withdrawn_at.is_(None),
            )
            .order_by(RequestRow.created_at)
        )

    async def of(self, actor_id: str, *, limit: int) -> list[SecurityRequest]:
        return await self._many(
            select(RequestRow)
            .join(
                SecurityRequestRequester,
                SecurityRequestRequester.request_id == RequestRow.id,
            )
            .where(SecurityRequestRequester.actor_id == uuid.UUID(actor_id))
            .order_by(RequestRow.created_at.desc())
            .limit(limit)
        )

    async def all(
        self, *, status: RequestStatus | None, open_only: bool, limit: int
    ) -> list[SecurityRequest]:
        query = select(RequestRow).order_by(RequestRow.created_at).limit(limit)
        if status is not None:
            query = query.where(RequestRow.status == status.value)
        if open_only:
            query = query.where(RequestRow.open)
        return await self._many(query)

    async def ingestion_state(self, ingestion_id: str) -> IngestionState | None:
        key = _uuid(ingestion_id)
        if key is None:
            return None
        row = (
            await self._session.execute(
                select(SecurityIngestion).where(SecurityIngestion.id == key)
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        await self._session.refresh(row)
        return IngestionState(
            status=row.status,
            error_code=row.error_code,
            build_error=row.dataset_build_error,
        )

    async def dataset_of(self, ingestion_id: str) -> DatasetState | None:
        key = _uuid(ingestion_id)
        if key is None:
            return None
        row = (
            await self._session.execute(
                select(SecurityDataset)
                .where(SecurityDataset.ingestion_id == key)
                .order_by(SecurityDataset.built_at.desc())
                .limit(1)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        return _dataset(row) if row else None

    async def latest_published(self, code: SecurityCode) -> DatasetState | None:
        row = (
            await self._session.execute(
                select(SecurityDataset)
                .where(
                    SecurityDataset.security_code == code.code,
                    SecurityDataset.status == "published",
                )
                .order_by(SecurityDataset.built_at.desc())
                .limit(1)
                .execution_options(populate_existing=True)
            )
        ).scalar_one_or_none()
        return _dataset(row) if row else None

    async def start_ingestion(self, code: SecurityCode) -> str:
        return await self._start(self._session, code)

    async def commit(self) -> None:
        await self._session.commit()


class SqlWatchlist:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _row(self, code: SecurityCode, *, for_update: bool = False):
        query = select(SecurityWatchlist).where(
            SecurityWatchlist.security_code == code.code
        )
        if for_update:
            query = query.with_for_update()
        return (await self._session.execute(query)).scalar_one_or_none()

    async def contains(self, code: SecurityCode) -> bool:
        row = await self._row(code)
        return row is not None and row.removed_at is None

    async def entries(self, *, include_removed: bool = False) -> list[WatchlistEntry]:
        query = select(SecurityWatchlist).order_by(SecurityWatchlist.security_code)
        if not include_removed:
            query = query.where(SecurityWatchlist.removed_at.is_(None))
        rows = (await self._session.execute(query)).scalars()
        return [
            WatchlistEntry(
                security_code=row.security_code,
                added_at=row.added_at,
                added_by=row.added_by,
                note=row.note,
                removed_at=row.removed_at,
                removed_by=row.removed_by,
                removal_note=row.removal_note,
            )
            for row in rows
        ]

    def _log(self, code: SecurityCode, action: str, by: str, note, at) -> None:
        self._session.add(
            SecurityWatchlistLog(
                security_code=code.code, action=action, actor=by[:64], note=note, at=at
            )
        )

    async def add(
        self, code: SecurityCode, *, by: str, note: str | None, at: datetime
    ) -> bool:
        row = await self._row(code, for_update=True)
        if row is not None and row.removed_at is None:
            return False
        if row is None:
            self._session.add(
                SecurityWatchlist(
                    security_code=code.code, added_at=at, added_by=by[:64], note=note
                )
            )
        else:
            row.added_at, row.added_by, row.note = at, by[:64], note
            row.removed_at = row.removed_by = row.removal_note = None
        self._log(code, "add", by, note, at)
        await self._session.flush()
        return True

    async def remove(
        self, code: SecurityCode, *, by: str, note: str | None, at: datetime
    ) -> bool:
        row = await self._row(code, for_update=True)
        if row is None or row.removed_at is not None:
            return False
        row.removed_at, row.removed_by, row.removal_note = at, by[:64], note
        self._log(code, "remove", by, note, at)
        await self._session.flush()
        return True
