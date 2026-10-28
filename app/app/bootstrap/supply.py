"""供给的装配（SDD 0045）：把渠道插件、取数边界、对象存储、knowbase 客户端接起来。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.supply.cninfo import CHANNEL_ID, CninfoCatalog
from app.application.supply.demands import DemandService
from app.application.supply.directory import SecurityDirectory
from app.application.supply.jobs import JobRunner
from app.infrastructure.external.knowbase_supply import get_knowbase_supply
from app.infrastructure.repositories.supply import SqlSupplyRepository
from app.infrastructure.repositories.supply_directory import SqlDirectoryRepository
from app.infrastructure.securities.fetcher import CrawlHttpFetcher
from app.infrastructure.securities.pdf_words import PdfPlumberWordsReader
from app.infrastructure.storage.object_storage import get_object_storage
from app.infrastructure.supply.adapters import (
    CrawlHttp,
    PdfPlumberPages,
    StorageObjects,
)
from core.config import get_settings


def build_demand_service(
    sessions: async_sessionmaker[AsyncSession],
) -> DemandService:
    return DemandService(SqlSupplyRepository(lambda: sessions), get_knowbase_supply())


def build_directory(sessions: async_sessionmaker[AsyncSession]) -> SecurityDirectory:
    return SecurityDirectory(SqlDirectoryRepository(lambda: sessions))


async def build_directory_refresh() -> int:
    from app.infrastructure.storage.postgres import get_postgres

    sessions = get_postgres().session_factory
    return await build_directory(sessions).refresh(CninfoCatalog(_crawl_http()))


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


def build_job_runner(sessions: async_sessionmaker[AsyncSession]) -> JobRunner:
    http = _crawl_http()
    return JobRunner(
        SqlSupplyRepository(lambda: sessions),
        {CHANNEL_ID: CninfoCatalog(http)},
        get_knowbase_supply(),
        StorageObjects(get_object_storage()),
        pdf_pages=PdfPlumberPages(),
        pdf_words=PdfPlumberWordsReader(),
    )
