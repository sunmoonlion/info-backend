"""把建好的数据集登记到知识服务（0008-info 段二 → 段三）。

只登记通过了质量检查的版本（F-INFO-07）。文件不经这里传：知识服务按登记的位置与
校验值自己去对象存储取。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from app.application.ports.securities import DatasetRecords, DatasetRegistrar
from app.domain.securities import SecurityCode
from app.domain.securities.registration import DatasetRecord, RegistrationError

logger = logging.getLogger(__name__)
PUBLISHED = "published"


@dataclass(frozen=True)
class RegistrationSummary:
    record_id: str
    dataset_id: str
    data_version: str
    security_code: str
    sha256: str
    registered: bool


class DatasetRegistrationService:
    def __init__(self, *, records: DatasetRecords, registrar: DatasetRegistrar) -> None:
        self._records = records
        self._registrar = registrar

    @property
    def configured(self) -> bool:
        return self._registrar.configured

    async def register_latest(self, raw_code: str) -> RegistrationSummary:
        code = SecurityCode(raw_code)
        record = await self._records.latest_published(code)
        if record is None:
            raise RegistrationError("no_published_dataset", retryable=False)
        return await self._register(record)

    async def register(self, record_id: str) -> RegistrationSummary:
        record = await self._records.get(record_id)
        if record is None:
            raise RegistrationError("dataset_not_found", retryable=False)
        return await self._register(record)

    async def _register(self, record: DatasetRecord) -> RegistrationSummary:
        if record.status != PUBLISHED:
            raise RegistrationError(
                "dataset_not_published", retryable=False, detail=record.status
            )
        if not self._registrar.configured:
            raise RegistrationError("registrar_not_configured", retryable=False)
        try:
            await self._registrar.register(record)
        except RegistrationError as exc:
            await self._records.mark_registration_failed(record.record_id, exc.code)
            raise
        await self._records.mark_registered(record.record_id)
        logger.info(
            "security dataset registered code=%s version=%s",
            record.security_code,
            record.data_version,
        )
        return RegistrationSummary(
            record_id=record.record_id,
            dataset_id=record.dataset_id,
            data_version=record.data_version,
            security_code=record.security_code,
            sha256=record.sha256,
            registered=True,
        )
