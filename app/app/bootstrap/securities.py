"""证券数据的组装：把应用层的服务和基础设施的实现接起来（0008-info）。

三步各是一个持久任务，前一步成功才排下一步：采集 → 建数据集 → 向知识服务登记。

应用层只认端口；具体用哪个取数实现、哪个存储，在这里定。命令行、管理接口、后台任务
都从这里拿服务。
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.securities.collectors import (
    CninfoDisclosureCollector,
    EastmoneyF10StatementCollector,
)
from app.application.securities.dataset_service import SecurityDatasetService
from app.application.securities.ingestion_service import SecurityIngestionService
from app.application.securities.registration_service import (
    DatasetRegistrationService,
)
from app.application.services.durable_tasks import enqueue_task
from app.domain.securities import SecurityCode
from app.infrastructure.external.knowledge_app import ServiceTokenProvider
from app.infrastructure.models.securities import SecurityIngestion
from app.infrastructure.securities import (
    CrawlHttpFetcher,
    KnowledgeDatasetRegistrar,
    PdfPlumberReportReader,
    SqlBatchReader,
    SqlDatasetRecords,
    SqlDatasetStore,
    SqlIngestionStore,
)
from app.infrastructure.storage.object_storage import get_object_storage
from core.config import get_settings

SECURITY_INGEST_TOPIC = "info.security.ingest.v1"
SECURITY_DATASET_BUILD_TOPIC = "info.security.dataset.build.v1"
SECURITY_DATASET_REGISTER_TOPIC = "info.security.dataset.register.v1"
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


def build_security_dataset_service(
    session_factory: async_sessionmaker[AsyncSession],
) -> SecurityDatasetService:
    storage = get_object_storage()
    return SecurityDatasetService(
        batches=SqlBatchReader(session_factory=session_factory, storage=storage),
        reports=PdfPlumberReportReader(),
        store=SqlDatasetStore(
            session_factory=session_factory, storage=storage, clock=_now
        ),
        today=lambda: datetime.now(_SHANGHAI).date(),
    )


@lru_cache(maxsize=1)
def _knowledge_tokens() -> ServiceTokenProvider:
    # 令牌缓存在进程内；每次组装服务都新建的话，每次登记都要重新换令牌
    return ServiceTokenProvider(get_settings())


def build_dataset_registration_service(
    session_factory: async_sessionmaker[AsyncSession],
) -> DatasetRegistrationService:
    settings = get_settings()
    enabled = settings.knowledge_app_dataset_enabled
    return DatasetRegistrationService(
        records=SqlDatasetRecords(session_factory=session_factory, clock=_now),
        registrar=KnowledgeDatasetRegistrar(
            url=settings.knowledge_app_dataset_url if enabled else None,
            tokens=_knowledge_tokens() if enabled else None,
            timeout_seconds=settings.knowledge_app_timeout_seconds,
        ),
    )


async def enqueue_dataset_build(session: AsyncSession, ingestion_id: str) -> None:
    await enqueue_task(
        session,
        topic=SECURITY_DATASET_BUILD_TOPIC,
        key=ingestion_id,
        payload={"ingestion_id": ingestion_id},
        deduplication_key=f"info.security.dataset.build:{ingestion_id}:v1",
    )


async def enqueue_dataset_registration(session: AsyncSession, record_id: str) -> None:
    await enqueue_task(
        session,
        topic=SECURITY_DATASET_REGISTER_TOPIC,
        key=record_id,
        payload={"record_id": record_id},
        deduplication_key=f"info.security.dataset.register:{record_id}:v1",
    )
