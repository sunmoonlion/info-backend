"""采集申请的组装：把数据库里的账本、关注清单、登记采集批次接到申请服务上。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.application.securities.request_service import SecurityRequestService
from app.bootstrap.securities import start_security_ingestion
from app.domain.securities import SecurityCode
from app.infrastructure.securities.request_store import SqlRequestStore, SqlWatchlist
from core.config import get_settings


def _now() -> datetime:
    return datetime.now(UTC)


def build_security_request_service(
    session: AsyncSession, session_factory: async_sessionmaker[AsyncSession]
) -> SecurityRequestService:
    """用调用方这一次请求的数据库会话。服务里的每个动作自己提交。"""
    settings = get_settings()

    async def start(active: AsyncSession, code: SecurityCode) -> str:
        batch = await start_security_ingestion(active, session_factory, code)
        return str(batch.id)

    return SecurityRequestService(
        store=SqlRequestStore(session, start_ingestion=start),
        watchlist=SqlWatchlist(session),
        clock=_now,
        known_apps=frozenset(settings.security_request_sources()),
        max_open=settings.security_request_max_open,
        registration_enabled=settings.knowledge_app_dataset_enabled,
    )
