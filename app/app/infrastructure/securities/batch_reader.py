"""读回一个采集批次的条目与原文。"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.ports.securities import BatchItem, LoadedBatch
from app.domain.securities import IngestionStatus, SecurityCode
from app.infrastructure.models.info import RawArtifact
from app.infrastructure.models.securities import (
    SecurityIngestion,
    SecurityIngestionItem,
)
from app.infrastructure.storage.object_storage import ObjectStorage


@dataclass(frozen=True)
class _Locator:
    object_key: str
    version_id: str | None


class SqlBatchReader:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        storage: ObjectStorage,
    ) -> None:
        self._sessions = session_factory
        self._storage = storage

    async def load(self, ingestion_id: str) -> LoadedBatch | None:
        try:
            key = uuid.UUID(ingestion_id)
        except ValueError:
            return None
        async with self._sessions() as session:
            batch = await session.get(SecurityIngestion, key)
            if batch is None:
                return None
            rows = (
                await session.execute(
                    select(SecurityIngestionItem, RawArtifact)
                    .join(
                        RawArtifact,
                        RawArtifact.id == SecurityIngestionItem.raw_artifact_id,
                    )
                    .where(SecurityIngestionItem.ingestion_id == key)
                    .order_by(SecurityIngestionItem.seq)
                )
            ).all()
            return LoadedBatch(
                ingestion_id=str(batch.id),
                code=SecurityCode(batch.security_code),
                status=batch.status,
                items=tuple(
                    BatchItem(
                        seq=item.seq,
                        source=item.source_code,
                        kind=item.kind,
                        sha256=item.sha256,
                        http_status=item.http_status,
                        meta=dict(item.meta or {}),
                        locator=_Locator(artifact.object_key, artifact.version_id),
                    )
                    for item, artifact in rows
                ),
            )

    async def latest_succeeded(self, code: SecurityCode) -> str | None:
        async with self._sessions() as session:
            found = (
                await session.execute(
                    select(SecurityIngestion.id)
                    .where(
                        SecurityIngestion.security_code == code.code,
                        SecurityIngestion.status == IngestionStatus.SUCCEEDED.value,
                    )
                    .order_by(SecurityIngestion.requested_at.desc())
                    .limit(1)
                )
            ).scalar_one_or_none()
            return str(found) if found else None

    async def read(self, item: BatchItem) -> bytes:
        locator: _Locator = item.locator
        return await asyncio.to_thread(
            self._storage.get_bytes,
            object_key=locator.object_key,
            version_id=locator.version_id,
            expected_sha256=item.sha256,
        )
