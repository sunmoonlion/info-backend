import logging
from functools import lru_cache
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from core.config import get_settings

logger = logging.getLogger(__name__)


class Postgres:
    def __init__(self):
        self._engine: Optional[AsyncEngine] = None
        self._session_factory: Optional[async_sessionmaker] = None
        self._settings = get_settings()

    async def init(self) -> None:
        if self._engine is not None:
            logger.warning("Postgres引擎已初始化，无需重复操作")
            return
        try:
            logger.info("正在初始化Postgres连接...")
            self._engine = create_async_engine(
                self._settings.sqlalchemy_database_uri,
                echo=self._settings.env == "development",
                pool_pre_ping=True,
            )
            self._session_factory = async_sessionmaker(
                autocommit=False,
                autoflush=False,
                bind=self._engine,
            )
            async with self._engine.begin() as conn:
                await conn.execute(text('CREATE EXTENSION IF NOT EXISTS "uuid-ossp";'))
            logger.info("Postgres初始化成功")
        except Exception as e:
            logger.error(f"Postgres初始化失败: {e}")
            raise

    async def shutdown(self) -> None:
        if self._engine:
            await self._engine.dispose()
            self._engine = None
            self._session_factory = None
            logger.info("Postgres连接已关闭")
        get_postgres.cache_clear()

    @property
    def session_factory(self) -> async_sessionmaker[AsyncSession]:
        if self._session_factory is None:
            raise RuntimeError("Postgres未初始化")
        return self._session_factory


@lru_cache()
def get_postgres() -> Postgres:
    return Postgres()


async def get_db_session():
    async with get_postgres().session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
