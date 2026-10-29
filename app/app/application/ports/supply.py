"""供给的端口（SDD 0045）：记录、knowbase、渠道、对象存储、给应用的状态推送。"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Protocol

from app.domain.supply.catalog import ChannelEvent, Trial
from app.domain.supply.models import (
    Channel,
    Demand,
    DemandItem,
    DescriptionKey,
    Entry,
    Job,
    Original,
)
from app.domain.supply.quota import Quota
from app.domain.supply.schedule import Usage


class SupplyRepository(Protocol):
    async def channels(self) -> list[Channel]: ...

    async def get_channel(self, channel_id: str) -> Channel | None: ...

    async def find_demand(self, app: str, idempotency_key: str) -> Demand | None: ...

    async def get_demand(self, demand_id: str) -> Demand | None: ...

    async def items_of(self, demand_id: str) -> list[DemandItem]: ...

    async def submit(
        self,
        demand: Demand,
        items: list[DemandItem],
        jobs: list[Job],
        links: dict[str, str],
        *,
        quota: Quota,
    ) -> list[Job]:
        """一个事务里核配额、记需求单、需求项、新作业（已有的未办完作业照用），并排进队列。

        配额按归属用户在同一个事务里数（同一用户并发提单也不会一起超），超了抛
        `SupplyError`，什么都不记。`links`：需求项编号 → 作业的去重键。返回新排的作业。
        """
        ...

    async def move_items(
        self, moves: list[tuple[str, Job, tuple[str, ...], str]], at: datetime
    ) -> None:
        """取不到换下一个渠道：（需求项，下一个渠道的作业，试过的渠道，上一条为什么没取到）。"""
        ...

    async def defer(self, job_id: str, until: datetime, reason: str) -> None:
        """渠道这会儿不能再问：放回队列，`until` 以后再排，不算一次失败。"""
        ...

    async def delivered_original(
        self, channel_id: str, channel_doc_id: str
    ) -> tuple[Original, list[str]] | None: ...

    async def hold_job(self, job_id: str) -> None:
        """渠道暂停：作业不跑、回到等待，恢复渠道时再排。"""
        ...

    async def channel_failed(
        self, channel_id: str, at: datetime, *, pause_after: int
    ) -> bool:
        """渠道这次没取到（对方出错、取数失败）：连续失败次数加一，到了次数自动暂停、
        记一条审核记录。回是不是这次暂停的。"""
        ...

    async def channel_succeeded(self, channel_id: str) -> None:
        """渠道这次答了（有或没有都算）：连续失败次数清零。"""
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
        readings: list[str] | None = None,
    ) -> None: ...

    async def cancel_items(self, demand_id: str, at: datetime, reason: str) -> int: ...

    async def revive_original(self, original_id: str) -> None:
        """内容删过的原件又取回来了（重新取到同一份）：内容已重新放回，清掉删除记录。"""
        ...


class ScheduleRepository(Protocol):
    """调度（0045 第 3.3 节）：排作业、限速、预计时间用的队列数、加工作业。"""

    async def dispatch(self, now: datetime, *, process_slots: int) -> list[str]: ...

    async def take(
        self,
        channel_id: str,
        *,
        fetch: bool,
        rate_per_minute: int,
        daily_cap: int,
        global_rate: int,
        now: datetime,
    ) -> datetime | None: ...

    async def usage(self, channel_id: str, now: datetime) -> Usage: ...

    async def queue_ahead(self, tier: int) -> dict[str, int]: ...

    async def fetched_today(self, now: datetime) -> dict[str, int]: ...

    async def add_process_jobs(
        self, pending: list[tuple[str, str, int]], now: datetime
    ) -> int: ...

    async def prune_requests(self, before: datetime) -> int: ...


class CatalogRepository(Protocol):
    """来源目录（0045 第 3.1 节）：渠道、审核记录、试采、待审网站。"""

    async def channels(self) -> list[Channel]: ...

    async def get_channel(self, channel_id: str) -> Channel | None: ...

    async def add_channel(self, channel: Channel, event: ChannelEvent) -> None:
        """编号或网站已登记过的拒绝（409）。"""
        ...

    async def save_channel(
        self,
        channel: Channel,
        event: ChannelEvent,
        *,
        withdraw: str | None = None,
        requeue: bool = False,
    ) -> None:
        """一个事务里改渠道、记审核记录。`withdraw`：渠道上没办完的作业取消，在等的需求项
        转「无法满足」附这个原因（并通知应用）；`requeue`：等着的作业马上再排。"""
        ...

    async def reorder(
        self, priorities: dict[str, int], events: list[ChannelEvent], at: datetime
    ) -> None: ...

    async def events(self, channel_id: str, *, limit: int) -> list[ChannelEvent]: ...

    async def registration_changed_at(self, channel_id: str) -> datetime | None:
        """最近一次登记或改登记的时间（试采要比它新才算数）。"""
        ...

    async def trials(self, channel_id: str, *, limit: int) -> list[Trial]: ...

    async def latest_trial(self, channel_id: str) -> Trial | None: ...

    async def add_trial(self, trial: Trial) -> None:
        """记一次试采并排给 worker。"""
        ...

    async def start_trial(self, trial_id: str, at: datetime) -> Trial | None:
        """排着的改成在跑；已经跑完的回 None。"""
        ...

    async def finish_trial(
        self,
        trial_id: str,
        at: datetime,
        *,
        status: str,
        listed: list[dict[str, object]] | None = None,
        fetched: dict[str, object] | None = None,
        error: str | None = None,
    ) -> None: ...

    async def channel_for_host(self, host: str) -> Channel | None: ...

    async def add_site(self, channel: Channel, event: ChannelEvent) -> Channel:
        """自动建一条待审网站；同时有人建了同一个网站就回已有的那条。"""
        ...

    async def pending_sites(self) -> list[dict[str, object]]: ...

    async def expire_sites(self, before: datetime, at: datetime) -> int:
        """待审在 `before` 之前就开始的网站：在等的需求项转「无法满足」。回转了几项。"""
        ...


class MonitorRepository(Protocol):
    async def gaps(self, kind: str | None) -> list[dict[str, object]]: ...

    async def queue(self, since: datetime) -> dict[str, object]: ...

    async def jobs(
        self, *, channel_id: str | None, status: str | None, limit: int
    ) -> list[dict[str, object]]: ...

    async def job(self, job_id: str) -> dict[str, object] | None: ...

    async def requeue(self, job_id: str) -> None: ...


class RetentionRepository(Protocol):
    async def due_originals(self, now: datetime, limit: int) -> list[tuple[str, str]]:
        """到期、内容还没删的原件：（编号，对象键）。"""
        ...

    async def mark_purged(self, original_id: str, at: datetime) -> bool: ...


class StatusSender(Protocol):
    """把需求状态通知交给提单的应用（契约 knowfeed-supply-status/v1）。"""

    def configured(self, app: str) -> bool: ...

    async def send(self, app: str, body: dict[str, object]) -> None: ...


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
        subject: str,
        doc_type: str,
        period_start: date | None,
        period_end: date | None,
        edition: str,
        topics: tuple[str, ...],
        disclosed_on: str | None,
        source_url: str,
    ) -> dict[str, Any]:
        """交一份公开原件；描述是这一份的（细类、版本形态、事项，SDD 0049）。"""
        ...

    async def add_references(
        self, material_id: str, version_no: int, references: list[tuple[str, str]]
    ) -> int: ...

    async def processing_grant(
        self, material_id: str, version_no: int, *, owner: str = ...
    ) -> str:
        """领一条加工授权：公开原件的所有者是 `public:disclosure`，上传的是本人。"""
        ...

    async def pending_processing(self, limit: int) -> list[tuple[str, str, int]]:
        """knowbase 的待加工清单：（所有者，资料，版本）。"""
        ...

    async def write_text(
        self,
        grant_id: str,
        processor_version: str,
        body: str,
        *,
        title: str | None,
        published_on: date | None,
    ) -> dict[str, Any]: ...

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


class KnowbaseReader(Protocol):
    """凭加工授权读 knowbase 的原件（关系 `user-data-read`，0045 第 3.4 节「重新加工（拉）」：
    经 knowbase 接口取原件，不碰桶）。"""

    async def content(
        self, grant_id: str, material_id: str, version_no: int
    ) -> tuple[bytes, str]:
        """（内容，内容类型）。"""
        ...

    async def release(self, grant_id: str) -> None: ...


class ObjectStore(Protocol):
    async def put(self, key: str, content: bytes, media_type: str) -> None: ...

    async def get(self, key: str) -> bytes: ...

    async def delete(self, key: str) -> None:
        """真删这个键的内容（桶开了版本的，每个版本都删）；早就不在了也算删了。"""
        ...
