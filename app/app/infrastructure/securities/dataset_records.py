"""数据集登记记录的读取与「已向下游登记」的标记。"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.securities import SecurityCode
from app.domain.securities.registration import DatasetRecord
from app.infrastructure.models.securities import SecurityDataset

_PUBLISHED = "published"


def _record(row: SecurityDataset) -> DatasetRecord:
    return DatasetRecord(
        record_id=str(row.id),
        dataset_id=row.dataset_id,
        data_version=row.data_version,
        security_code=row.security_code,
        status=row.status,
        ingestion_id=str(row.ingestion_id),
        bucket=row.bucket,
        object_key=row.object_key,
        version_id=row.version_id,
        sha256=row.sha256,
        size_bytes=row.size_bytes,
        start_date=row.start_date,
        end_date=row.end_date,
        registered_at=row.knowledge_registered_at,
        registration_error=row.knowledge_registration_error,
    )


class SqlDatasetRecords:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        clock: Callable[[], datetime],
    ) -> None:
        self._sessions = session_factory
        self._clock = clock

    async def get(self, record_id: str) -> DatasetRecord | None:
        try:
            key = uuid.UUID(record_id)
        except (ValueError, AttributeError, TypeError):
            return None
        async with self._sessions() as session:
            row = await session.get(SecurityDataset, key)
            return _record(row) if row else None

    async def latest_published(self, code: SecurityCode) -> DatasetRecord | None:
        async with self._sessions() as session:
            row = (
                await session.execute(
                    select(SecurityDataset)
                    .where(
                        SecurityDataset.security_code == code.code,
                        SecurityDataset.status == _PUBLISHED,
                    )
                    .order_by(SecurityDataset.built_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            return _record(row) if row else None

    async def mark_registered(self, record_id: str) -> None:
        async with self._sessions() as session:
            row = await session.get(SecurityDataset, uuid.UUID(record_id))
            if row is None:
                raise RuntimeError("dataset_record_missing")
            row.knowledge_registered_at = self._clock()
            row.knowledge_registration_error = None
            await session.commit()

    async def mark_registration_failed(self, record_id: str, code: str) -> None:
        async with self._sessions() as session:
            row = await session.get(SecurityDataset, uuid.UUID(record_id))
            if row is None:
                raise RuntimeError("dataset_record_missing")
            row.knowledge_registration_error = code[:80]
            await session.commit()
