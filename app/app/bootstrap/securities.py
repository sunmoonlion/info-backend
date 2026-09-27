"""证券采集的组装：把应用层的采集服务和基础设施的实现接起来（0008-info 段一）。

应用层只认端口；具体用哪个取数实现、哪个存储，在这里定。命令行、管理接口、后台任务
都从这里拿服务。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.securities.collectors import (
    CninfoDisclosureCollector,
    EastmoneyF10StatementCollector,
)
from app.application.securities.ingestion_service import SecurityIngestionService
from app.application.services.durable_tasks import enqueue_task
from app.domain.securities import SecurityCode
from app.infrastructure.models.securities import SecurityIngestion
from app.infrastructure.securities import CrawlHttpFetcher, SqlIngestionStore
from app.infrastructure.storage.object_storage import get_object_storage
from core.config import get_settings

SECURITY_INGEST_TOPIC = "info.security.ingest.v1"
_SHANGHAI = timezone(timedelta(hours=8))
_PAUSE_SECONDS = 0.3


def _now() -> datetime:
    return datetime.now(UTC)


def build_store(
    session_factory: async_sessionmaker[AsyncSession],
) -> SqlIngestionStore:
    return SqlIngestionStore(
        session_factory=session_factory, storage=get_object_storage(), clock=_now
    )


def build_security_ingestion_service(
    session_factory: async_sessionmaker[AsyncSession],
) -> SecurityIngestionService:
    settings = get_settings()

    async def pause() -> None:
        await asyncio.sleep(_PAUSE_SECONDS)

    async def backoff(attempt: int) -> None:
        await asyncio.sleep(2.0**attempt)  # 2 秒、4 秒

    return SecurityIngestionService(
        fetcher=CrawlHttpFetcher(
            timeout_seconds=settings.crawl_timeout_seconds,
            max_bytes=settings.crawl_max_bytes,
            user_agent=settings.crawl_user_agent,
        ),
        store=build_store(session_factory),
        collectors=[
            CninfoDisclosureCollector(today=datetime.now(_SHANGHAI).date()),
            EastmoneyF10StatementCollector(),
        ],
        clock=_now,
        pause=pause,
        backoff=backoff,
    )


async def request_security_ingestion(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    raw_code: str,
) -> SecurityIngestion:
    """登记批次并排队执行；两件事在同一个事务里提交。"""
    code = SecurityCode(raw_code)
    service = build_security_ingestion_service(session_factory)
    batch = await build_store(session_factory).start_in(
        session, code, sources=service.sources
    )
    await enqueue_task(
        session,
        topic=SECURITY_INGEST_TOPIC,
        key=str(batch.id),
        payload={"ingestion_id": str(batch.id)},
        deduplication_key=f"info.security.ingest:{batch.id}:v1",
    )
    await session.commit()
    await session.refresh(batch)
    return batch
