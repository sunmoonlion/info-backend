"""供给的装配（SDD 0045）：把渠道插件、取数边界、限速、对象存储、knowbase 客户端接起来。"""

from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.ports.supply import Catalog
from app.application.supply.catalog import ChannelCatalog, TrialRunner
from app.application.supply.cninfo import CninfoCatalog, Http
from app.application.supply.cninfo_irm import CninfoIrmCatalog
from app.application.supply.demands import DemandService
from app.application.supply.directory import SecurityDirectory
from app.application.supply.jobs import JobRunner
from app.application.supply.matching import PreMatch
from app.application.supply.monitor import SupplyMonitor
from app.application.supply.notify import StatusNotifier
from app.application.supply.retention import OriginalRetention
from app.application.supply.scheduler import (
    ChannelLimiter,
    ProcessingIntake,
    Scheduler,
)
from app.domain.supply.models import Channel
from app.domain.supply.quota import Quota
from app.infrastructure.external.app_supply_status import HttpStatusSender
from app.infrastructure.external.knowbase_supply import (
    HttpKnowbaseReader,
    get_knowbase_reader,
    get_knowbase_supply,
    read_relation,
)
from app.infrastructure.repositories.supply import SqlSupplyRepository
from app.infrastructure.repositories.supply_catalog import SqlCatalogRepository
from app.infrastructure.repositories.supply_directory import SqlDirectoryRepository
from app.infrastructure.repositories.supply_schedule import SqlScheduleRepository
from app.infrastructure.securities.fetcher import CrawlHttpFetcher
from app.infrastructure.securities.pdf_words import PdfPlumberWordsReader
from app.infrastructure.storage.object_storage import get_object_storage
from app.infrastructure.supply.adapters import (
    CrawlHttp,
    LimitedHttp,
    PdfPlumberPages,
    StorageObjects,
)
from core.config import get_settings

# 渠道插件：插件名 → 按渠道建适配器（给它这条渠道的、经限速的 Http）。加渠道是登记一条
# 记录、选现有的插件；通用网页型等随块 3 加进来（同时改 `catalog.PLUGINS`）
PLUGIN_FACTORIES: dict[str, Callable[[Http, Channel], Catalog]] = {
    "cninfo": lambda http, channel: CninfoCatalog(http),
    "cninfo-irm": lambda http, channel: CninfoIrmCatalog(http, channel.id),
}


class ChannelPlugins:
    """按渠道建插件：插件不自带网络客户端，用框架给的、按这条渠道限速的 Http。"""

    def __init__(self, http: Http, limiter: ChannelLimiter) -> None:
        self._http = http
        self._limiter = limiter

    def __call__(self, channel: Channel) -> Catalog | None:
        factory = PLUGIN_FACTORIES.get(channel.plugin)
        if factory is None:
            return None
        return factory(LimitedHttp(self._http, self._limiter, channel), channel)


def quota() -> Quota:
    settings = get_settings()
    return Quota(
        per_hour=settings.supply_quota_per_hour,
        open_demands=settings.supply_quota_open_demands,
        monthly_items=settings.supply_quota_monthly_items,
        subscriptions=settings.supply_quota_subscriptions,
    )


def build_demand_service(
    sessions: async_sessionmaker[AsyncSession],
) -> DemandService:
    return DemandService(
        SqlSupplyRepository(lambda: sessions), get_knowbase_supply(), quota=quota()
    )


def build_prematch(sessions: async_sessionmaker[AsyncSession]) -> PreMatch:
    return PreMatch(
        SqlSupplyRepository(lambda: sessions),
        SqlCatalogRepository(lambda: sessions),
        SqlScheduleRepository(lambda: sessions),
        global_rate=get_settings().supply_global_rate_per_minute,
    )


def build_catalog(sessions: async_sessionmaker[AsyncSession]) -> ChannelCatalog:
    return ChannelCatalog(SqlCatalogRepository(lambda: sessions))


def build_monitor(sessions: async_sessionmaker[AsyncSession]) -> SupplyMonitor:
    return SupplyMonitor(SqlCatalogRepository(lambda: sessions))


def build_trial_runner(sessions: async_sessionmaker[AsyncSession]) -> TrialRunner:
    return TrialRunner(SqlCatalogRepository(lambda: sessions), _plugins(sessions))


def build_retention(sessions: async_sessionmaker[AsyncSession]) -> OriginalRetention:
    return OriginalRetention(
        SqlCatalogRepository(lambda: sessions), StorageObjects(get_object_storage())
    )


def build_notifier() -> StatusNotifier:
    return StatusNotifier(HttpStatusSender())


def build_directory(sessions: async_sessionmaker[AsyncSession]) -> SecurityDirectory:
    return SecurityDirectory(SqlDirectoryRepository(lambda: sessions))


async def build_directory_refresh() -> int:
    from app.infrastructure.storage.postgres import get_postgres

    sessions = get_postgres().session_factory
    return await build_directory(sessions).refresh(CninfoCatalog(_crawl_http()))


def build_scheduler(sessions: async_sessionmaker[AsyncSession]) -> Scheduler:
    return Scheduler(
        SqlScheduleRepository(lambda: sessions),
        process_slots=get_settings().supply_process_max_running,
    )


def build_processing_intake(
    sessions: async_sessionmaker[AsyncSession],
) -> ProcessingIntake:
    return ProcessingIntake(
        get_knowbase_supply(), SqlScheduleRepository(lambda: sessions)
    )


def _crawl_http() -> CrawlHttp:
    settings = get_settings()
    return CrawlHttp(
        CrawlHttpFetcher(
            timeout_seconds=settings.crawl_timeout_seconds,
            max_bytes=settings.crawl_max_bytes,
            user_agent=settings.crawl_user_agent,
            max_bytes_ceiling=settings.security_report_max_bytes,
            timeout_ceiling_seconds=settings.security_report_timeout_seconds,
        )
    )


def _plugins(sessions: async_sessionmaker[AsyncSession]) -> ChannelPlugins:
    limiter = ChannelLimiter(
        SqlScheduleRepository(lambda: sessions),
        global_rate=get_settings().supply_global_rate_per_minute,
    )
    return ChannelPlugins(_crawl_http(), limiter)


def _reader() -> HttpKnowbaseReader | None:
    """凭授权读原件的关系没配时不读：上传资料的加工作业照实失败（`reader_not_configured`）。"""
    return get_knowbase_reader() if read_relation() is not None else None


def build_job_runner(sessions: async_sessionmaker[AsyncSession]) -> JobRunner:
    return JobRunner(
        SqlSupplyRepository(lambda: sessions),
        _plugins(sessions),
        get_knowbase_supply(),
        StorageObjects(get_object_storage()),
        pdf_pages=PdfPlumberPages(),
        pdf_words=PdfPlumberWordsReader(),
        reader=_reader(),
    )
