"""数据集的留存与登记：文件、清单、质量报告进对象存储，登记进 PostgreSQL。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.securities.dataset import BuiltDataset
from app.infrastructure.models.securities import SecurityDataset
from app.infrastructure.storage.object_storage import ObjectStorage


def dataset_prefix(*, code: str, data_version: str) -> str:
    return f"info/securities/code={code}/datasets/{data_version}"


class SqlDatasetStore:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        storage: ObjectStorage,
        clock: Callable[[], datetime],
    ) -> None:
        self._sessions = session_factory
        self._storage = storage
        self._clock = clock

    async def save(self, dataset: BuiltDataset, *, ingestion_id: str) -> str:
        async with self._sessions() as session:
            existing = (
                await session.execute(
                    select(SecurityDataset).where(
                        SecurityDataset.security_code == dataset.security_code,
                        SecurityDataset.data_version == dataset.data_version,
                    )
                )
            ).scalar_one_or_none()
            if existing is not None:
                if existing.sha256 != dataset.sha256:
                    raise RuntimeError("dataset_version_content_mismatch")
                return str(existing.id)
            prefix = dataset_prefix(
                code=dataset.security_code, data_version=dataset.data_version
            )
            quality = dataset.quality.as_dict()
            manifest = {
                "dataset_id": dataset.dataset_id,
                "data_version": dataset.data_version,
                "security_code": dataset.security_code,
                "status": dataset.status,
                "file": f"{dataset.dataset_id}.sqlite",
                "sha256": dataset.sha256,
                "size_bytes": len(dataset.content),
                "row_counts": dataset.row_counts,
                "start_date": dataset.start_date,
                "end_date": dataset.end_date,
                "source_ingestion_id": ingestion_id,
                "quality_passed": dataset.quality.passed,
                "failed_checks": list(dataset.quality.failed_blocking),
            }
            stored = self._storage.put_bytes(
                object_key=f"{prefix}/{dataset.dataset_id}.sqlite",
                data=dataset.content,
                content_type="application/vnd.sqlite3",
                metadata={
                    "data_version": dataset.data_version,
                    "status": dataset.status,
                },
            )
            if stored.sha256 != dataset.sha256:
                raise RuntimeError("stored object digest mismatch")
            self._storage.put_json(
                object_key=f"{prefix}/manifest.json", payload=manifest
            )
            self._storage.put_json(object_key=f"{prefix}/quality.json", payload=quality)
            record = SecurityDataset(
                security_code=dataset.security_code,
                dataset_id=dataset.dataset_id,
                data_version=dataset.data_version,
                status=dataset.status,
                ingestion_id=uuid.UUID(ingestion_id),
                bucket=stored.bucket,
                object_key=stored.object_key,
                version_id=stored.version_id,
                sha256=stored.sha256,
                size_bytes=stored.size_bytes,
                row_counts=dataset.row_counts,
                quality=json.loads(json.dumps(quality, ensure_ascii=False)),
                metadata_json=dict(dataset.metadata),
                start_date=dataset.start_date,
                end_date=dataset.end_date,
                built_at=self._clock(),
            )
            session.add(record)
            await session.flush()
            record_id = str(record.id)
            await session.commit()
            return record_id
