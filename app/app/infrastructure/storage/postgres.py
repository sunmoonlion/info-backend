import logging
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from core.config import get_settings

logger = logging.getLogger(__name__)


class Postgres:
    def __init__(self):
        self._engine: AsyncEngine | None = None
        self._session_factory: async_sessionmaker | None = None
        self._settings = get_settings()

    async def init(self) -> None:
        if self._engine is not None:
            logger.warning("Postgres引擎已初始化，无需重复操作")
            return
        try:
            logger.info("正在初始化Postgres连接...")
            self._engine = create_async_engine(
                self._settings.database_url,
                echo=self._settings.env == "development",
                pool_pre_ping=True,
            )
            self._session_factory = async_sessionmaker(
                autocommit=False,
                autoflush=False,
                bind=self._engine,
            )
            logger.info("Postgres初始化成功")
        except Exception as exc:
            logger.error("postgres_initialization_failed type=%s", type(exc).__name__)
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


@lru_cache
def get_postgres() -> Postgres:
    return Postgres()


async def get_db_session():
    async with get_postgres().session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
