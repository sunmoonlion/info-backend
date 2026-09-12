from __future__ import annotations

import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.storage.crawl_concurrency import (
    CrawlSourceBusy,
    crawl_source_slot,
    source_lock_key,
)


@pytest.mark.parametrize(
    "left,right",
    [
        ("https://EXAMPLE.com/a?token=x", "http://example.com./b#fragment"),
        ("https://例子.com/feed", "https://xn--fsqu00a.com/post"),
        ("https://[2001:4860:4860::8888]/", "http://[2001:4860:4860:0:0:0:0:8888]/"),
    ],
)
def test_anonymous_sources_use_normalized_host(left, right):
    assert source_lock_key(None, left) == source_lock_key(None, right)


def test_explicit_source_is_stable_and_distinct():
    source = uuid.UUID("51ade987-336d-4351-a355-71eb2a54d0df")
    key = source_lock_key(source, "https://one.example/a")
    assert key == source_lock_key(source, "https://two.example/b")
    assert key != source_lock_key(uuid.uuid4(), "https://one.example/a")
    assert key != source_lock_key(None, "https://one.example/a")
    assert -(2**63) <= key < 2**63


def test_missing_or_invalid_host_is_not_a_bypass():
    assert source_lock_key(None, "") == source_lock_key(None, "http://x:bad")


async def test_unbound_database_fails_closed():
    async with AsyncSession() as session:
        with pytest.raises(RuntimeError, match="requires_postgresql"):
            async with crawl_source_slot(session, source_id=None, url="https://e.test"):
                pytest.fail("must not admit without a database")


async def test_discovery_busy_maps_to_retryable_conflict(monkeypatch):
    from app.interfaces.endpoints import info_routes
    from app.interfaces.schemas.info import CollectorDiscoverRequest

    async def busy(*args, **kwargs):
        raise CrawlSourceBusy()

    monkeypatch.setattr(info_routes.info_crawl_service, "run_collector_discovery", busy)
    async with AsyncSession() as session:
        with pytest.raises(HTTPException) as raised:
            await info_routes.discover_collector(
                uuid.uuid4(), CollectorDiscoverRequest(), session
            )
    assert raised.value.status_code == 409
    assert raised.value.detail == "crawl_source_busy"
    assert raised.value.headers == {"Retry-After": "5"}
