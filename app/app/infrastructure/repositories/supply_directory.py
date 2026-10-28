"""证券名录的记录（PostgreSQL）：查主体、整份换、排刷新。"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.supply.directory import SecurityEntry
from app.infrastructure.messaging.durable_tasks import enqueue_task
from app.infrastructure.models.supply import SupplySecurity

REFRESH_TOPIC = "knowfeed.supply.directory.refresh.v1"


def _like(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class SqlDirectoryRepository:
    def __init__(self, sessions: Callable[[], async_sessionmaker[AsyncSession]]):
        self._sessions = sessions

    async def search(self, query: str, limit: int) -> list[SecurityEntry]:
        """代码（六位或带后缀）对上的在前，其次简称一字不差的，再次简称含它的、拼音
        首字母打头的；同档按简称短的在前。"""
        code = query.upper()
        lowered = query.lower()
        pattern = f"%{_like(query)}%"
        rank = case(
            (
                or_(
                    SupplySecurity.subject == code,
                    func.split_part(SupplySecurity.subject, ".", 1) == code,
                ),
                0,
            ),
            (SupplySecurity.name == query, 1),
            else_=2,
        )
        async with self._sessions()() as session:
            rows = await session.execute(
                select(SupplySecurity)
                .where(
                    or_(
                        SupplySecurity.subject == code,
                        func.split_part(SupplySecurity.subject, ".", 1) == code,
                        SupplySecurity.name.like(pattern, escape="\\"),
                        SupplySecurity.pinyin.like(f"{_like(lowered)}%", escape="\\"),
                    )
                )
                .order_by(
                    rank, func.length(SupplySecurity.name), SupplySecurity.subject
                )
                .limit(limit)
            )
            return [SecurityEntry(r.subject, r.name, r.pinyin) for r in rows.scalars()]

    async def refreshed_at(self) -> datetime | None:
        async with self._sessions()() as session:
            return await session.scalar(select(func.max(SupplySecurity.refreshed_at)))

    async def request_refresh(self, day: str) -> None:
        # 一天至多排一次（发件箱按去重键幂等）
        async with self._sessions()() as session, session.begin():
            await enqueue_task(
                session,
                topic=REFRESH_TOPIC,
                key="security-directory",
                payload={"day": day},
                deduplication_key=f"supply.directory:{day}",
            )

    async def replace(self, entries: list[SecurityEntry], at: datetime) -> int:
        unique = {e.subject: e for e in entries}
        async with self._sessions()() as session, session.begin():
            await session.execute(delete(SupplySecurity))
            rows = [
                {
                    "subject": e.subject,
                    "name": e.name,
                    "pinyin": e.pinyin,
                    "refreshed_at": at,
                }
                for e in unique.values()
            ]
            for start in range(0, len(rows), 1000):
                await session.execute(
                    insert(SupplySecurity).values(rows[start : start + 1000])
                )
        return len(unique)
