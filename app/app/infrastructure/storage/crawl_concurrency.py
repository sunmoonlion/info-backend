"""Info-only, cross-process source admission. Not a task scheduler or lease.

One execution per logical source (fallback: target hostname), across discovery
and crawling. A separate transaction keeps the advisory lock through domain
commits and releases it on rollback/connection close. Never wait for a busy key.
"""

from __future__ import annotations

import hashlib
import ipaddress
import uuid
from contextlib import asynccontextmanager

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession


class CrawlSourceBusy(RuntimeError):
    def __init__(self):
        super().__init__("crawl_source_busy")


def source_lock_key(source_id: uuid.UUID | None, url: str) -> int:
    if source_id is not None:
        identity = "source:" + str(source_id)
    else:
        try:
            host = httpx.URL(url).raw_host.decode("ascii").lower().rstrip(".")
        except (httpx.InvalidURL, ValueError):
            host = ""
        try:
            host = str(ipaddress.ip_address(host))
        except ValueError:
            pass  # DNS names remain lower-case ASCII/IDNA, without trailing dots.
        # Invalid URLs still reach the fetch validator and its durable failure
        # path. Never use a unique random key that would bypass source admission.
        identity = "host:" + host if host else "invalid-url"
    digest = hashlib.sha256(("info.crawl.source.v1:" + identity).encode()).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


@asynccontextmanager
async def crawl_source_slot(
    session: AsyncSession, *, source_id: uuid.UUID | None, url: str
):
    bind = getattr(session, "bind", None)
    engine = bind.engine if isinstance(bind, AsyncConnection) else bind
    if not isinstance(engine, AsyncEngine) or engine.dialect.name != "postgresql":
        raise RuntimeError("crawl_source_requires_postgresql")
    # Use a dedicated transaction, not the caller's connection: process_crawl_job
    # commits progress, and advisory xact locks are released by those commits.
    async with engine.connect() as connection, connection.begin():
        acquired = await connection.scalar(
            text("SELECT pg_try_advisory_xact_lock(:key)"),
            {"key": source_lock_key(source_id, url)},
        )
        if not acquired:
            raise CrawlSourceBusy()
        yield
