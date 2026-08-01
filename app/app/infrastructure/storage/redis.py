import logging
from functools import lru_cache

from redis.asyncio import Redis

from core.config import get_settings

logger = logging.getLogger(__name__)


class RedisClient:
    def __init__(self):
        self._client: Redis | None = None
        self._settings = get_settings()

    async def init(self) -> None:
        if self._client:
            logger.warning("Redis客户端已初始化，无需重复操作")
            return
        try:
            kw: dict = dict(
                host=self._settings.redis_host,
                port=self._settings.redis_port,
                db=self._settings.redis_db,
                password=self._settings.redis_password,
                decode_responses=True,
            )
            if self._settings.redis_user:
                kw["username"] = self._settings.redis_user
            self._client = Redis(**kw)
            await self._client.ping()  # type: ignore[misc]
            logger.info("Redis初始化成功")
        except Exception as exc:
            logger.error("redis_initialization_failed type=%s", type(exc).__name__)
            raise

    async def shutdown(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None
            logger.info("Redis连接已关闭")
        get_redis.cache_clear()

    @property
    def client(self) -> Redis:
        if self._client is None:
            raise RuntimeError("Redis未初始化")
        return self._client


@lru_cache
def get_redis() -> RedisClient:
    return RedisClient()
