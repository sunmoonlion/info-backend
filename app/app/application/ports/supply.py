"""供给的端口（SDD 0045）：记录、knowbase、渠道。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from app.domain.supply.models import (
    Channel,
    Demand,
    DemandItem,
    DescriptionKey,
    Entry,
    Job,
    Original,
)


class SupplyRepository(Protocol):
    async def channels(self) -> list[Channel]: ...

    async def find_demand(self, app: str, idempotency_key: str) -> Demand | None: ...

    async def get_demand(self, demand_id: str) -> Demand | None: ...

    async def items_of(self, demand_id: str) -> list[DemandItem]: ...

    async def submit(
        self,
        demand: Demand,
        items: list[DemandItem],
        jobs: list[Job],
        links: dict[str, str],
    ) -> list[Job]:
        """一个事务里记需求单、需求项、新作业（已有的未办完作业照用），并排进队列。

        `links`：需求项编号 → 作业的去重键。返回新排的作业。
        """
        ...

    async def get_job(self, job_id: str) -> Job | None: ...

    async def start_attempt(self, job_id: str, at: datetime) -> str | None:
        """作业开跑：排队中的改成跑着、记一次尝试；已结束的返回 None。"""
        ...

    async def finish_attempt(
        self,
        job_id: str,
        attempt_id: str,
        at: datetime,
        *,
        outcome: str,
        reason: str | None,
        job_status: str,
    ) -> None: ...

    async def retry_later(self, job_id: str, not_before: datetime) -> None: ...

    async def waiting_items(self, job_id: str) -> list[tuple[DemandItem, Demand]]:
        """还在等这个作业的需求项（没结束的），连同各自的需求单。"""
        ...

    async def settle_items(
        self,
        item_ids: list[str],
        status: str,
        at: datetime,
        *,
        reason: str | None = None,
        receipt: dict[str, object] | None = None,
    ) -> int:
        """只往前走：结束了的不动；返回改了几项。"""
        ...

    async def find_original(self, sha256: str) -> Original | None: ...

    async def save_original(
        self, original: Original, meta: dict[str, object]
    ) -> Original: ...

    async def mark_delivered(
        self,
        original_id: str,
        at: datetime,
        purge_after: datetime,
        material_id: str,
        version_no: int,
    ) -> None: ...

    async def cancel_items(self, demand_id: str, at: datetime) -> int: ...


class Catalog(Protocol):
    async def list(self, key: DescriptionKey) -> list[Entry]: ...

    async def fetch(self, entry: Entry) -> tuple[bytes, str]: ...


class Knowbase(Protocol):
    """knowbase 的供给接口（关系 `user-data-supply`）。"""

    async def check_confirmation(
        self, confirmation_id: str, subject: str
    ) -> dict[str, Any]: ...

    async def deliver_public(
        self,
        *,
        channel: str,
        channel_doc_id: str,
        name: str,
        content: bytes,
        key: DescriptionKey,
        disclosed_on: str | None,
        source_url: str,
    ) -> dict[str, Any]: ...

    async def add_references(
        self, material_id: str, version_no: int, references: list[tuple[str, str]]
    ) -> int: ...

    async def processing_grant(self, material_id: str, version_no: int) -> str: ...

    async def write_pages(
        self, grant_id: str, processor_version: str, pages: list[dict[str, object]]
    ) -> dict[str, Any]: ...

    async def write_failed(
        self, grant_id: str, kind: str, processor_version: str, reason: str
    ) -> dict[str, Any]: ...

    async def write_facts(
        self,
        grant_id: str,
        processor_version: str,
        raw: list[dict[str, object]],
        standard: list[dict[str, object]],
    ) -> dict[str, Any]: ...


class ObjectStore(Protocol):
    async def put(self, key: str, content: bytes, media_type: str) -> None: ...

    async def get(self, key: str) -> bytes: ...
