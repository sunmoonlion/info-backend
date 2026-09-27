"""数据集向下游登记时用到的领域对象（0008-info 段二 → 段三）。"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class DatasetRecord:
    """一个已经留存并登记在 info 的数据集版本。"""

    record_id: str
    dataset_id: str
    data_version: str
    security_code: str
    status: str
    ingestion_id: str
    bucket: str
    object_key: str
    version_id: str | None
    sha256: str
    size_bytes: int
    start_date: str
    end_date: str
    registered_at: datetime | None
    registration_error: str | None = None

    @property
    def title(self) -> str:
        return f"{self.security_code} 财务报表"


class RegistrationError(Exception):
    """登记没有成功。code 是稳定错误码；retryable 表示过一会儿再试可能成功。"""

    def __init__(self, code: str, *, retryable: bool, detail: str | None = None):
        super().__init__(code if detail is None else f"{code}: {detail}")
        self.code = code
        self.retryable = retryable
        self.detail = detail
