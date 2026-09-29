"""Browser login transactions and sessions in Redis."""

from __future__ import annotations

from app.infrastructure.storage.redis import get_redis


class RedisSessionStore:
    """Looks the client up on every call: it is built before Redis is initialized."""

    async def create(self, key: str, value: str, *, ttl_seconds: int) -> bool:
        return bool(await get_redis().client.set(key, value, ex=ttl_seconds, nx=True))

    async def read(self, key: str) -> str | None:
        return await get_redis().client.get(key)

    async def take(self, key: str) -> str | None:
        return await get_redis().client.getdel(key)

    async def delete(self, key: str) -> None:
        await get_redis().client.delete(key)
