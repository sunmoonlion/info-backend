"""Real PostgreSQL admission, cancellation and durable retry; no Internet/S3."""

from __future__ import annotations

import asyncio
import json
import sys
import uuid

import httpx
import pytest
from test_durable_delivery_db import age_published, sql
from test_durable_delivery_db import db as db
from test_info_delivery_db import MemoryStorage

from app.application.collectors.base import CollectedLink
from app.application.services import info_crawl_service as service
from app.application.services.durable_tasks import DurableTasks
from app.infrastructure.messaging.delivery_handlers import get_delivery_handlers
from app.infrastructure.storage.crawl_concurrency import (
    CrawlSourceBusy,
    crawl_source_slot,
)


async def test_same_source_busy_other_source_admitted_even_after_domain_commit(db):
    source_id = uuid.uuid4()
    async with db() as first, db() as second:
        async with crawl_source_slot(first, source_id=source_id, url="https://a.test"):
            await first.commit()
            with pytest.raises(CrawlSourceBusy):
                async with crawl_source_slot(
                    second, source_id=source_id, url="https://b.test"
                ):
                    pytest.fail("same logical source must not enter")
            async with crawl_source_slot(
                second, source_id=uuid.uuid4(), url="https://a.test"
            ):
                pass
        async with crawl_source_slot(second, source_id=source_id, url="https://a.test"):
            pass


async def test_anonymous_same_host_is_busy_across_independent_connections(db):
    async with db() as first, db() as second:
        async with crawl_source_slot(
            first, source_id=None, url="https://EXAMPLE.com/a"
        ):
            with pytest.raises(CrawlSourceBusy):
                async with crawl_source_slot(
                    second, source_id=None, url="http://example.com./b?token=x"
                ):
                    pytest.fail("hostname aliases must share admission")


@pytest.mark.parametrize("failure", [RuntimeError, asyncio.CancelledError])
async def test_exception_and_cancellation_release_source_transaction(db, failure):
    source_id = uuid.uuid4()
    async with db() as first, db() as second:
        with pytest.raises(failure):
            async with crawl_source_slot(
                first, source_id=source_id, url="https://a.test"
            ):
                raise failure()
        async with crawl_source_slot(second, source_id=source_id, url="https://a.test"):
            pass


async def test_abrupt_process_exit_releases_source_lock(db):
    # Locks must not rely on this process's asyncio semaphore or Python finalizers.
    source_id = uuid.uuid4()
    script = """
import asyncio, os, sys, uuid
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from app.infrastructure.storage.crawl_concurrency import crawl_source_slot
async def main():
    engine = create_async_engine(os.environ['DELIVERY_TEST_DATABASE_URL'])
    async with AsyncSession(engine) as session:
        async with crawl_source_slot(session, source_id=uuid.UUID(sys.argv[1]), url='https://a.test'):
            print('held', flush=True)
            sys.stdin.buffer.read(1)
            os._exit(0)
asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        str(source_id),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        assert (
            await asyncio.wait_for(process.stdout.readline(), timeout=10) == b"held\n"
        )
        async with db() as s:
            with pytest.raises(CrawlSourceBusy):
                async with crawl_source_slot(
                    s, source_id=source_id, url="https://a.test"
                ):
                    pytest.fail("another process owns the slot")
        process.stdin.write(b"x")
        await process.stdin.drain()
        assert await asyncio.wait_for(process.wait(), timeout=5) == 0
        async with asyncio.timeout(5), db() as s:
            while True:
                try:
                    async with crawl_source_slot(
                        s, source_id=source_id, url="https://a.test"
                    ):
                        break
                except CrawlSourceBusy:
                    await asyncio.sleep(0.01)
    finally:
        if process.returncode is None:
            process.kill()
        await process.communicate()


class CrawlStorage(MemoryStorage):
    def put_json(self, *, object_key, payload):
        return self.put_bytes(
            object_key=object_key,
            data=json.dumps(payload).encode(),
            content_type="application/json",
        )


def fake_crawl(monkeypatch):
    calls = []

    async def fetch(url, **kwargs):
        calls.append(url)
        return httpx.Response(
            200,
            content=b"<html><title>Test</title><body>Article</body></html>",
            headers={"content-type": "text/html"},
            request=httpx.Request("GET", url),
        )

    monkeypatch.setattr(service, "fetch_crawl_url", fetch)
    monkeypatch.setattr(service, "get_object_storage", CrawlStorage)
    monkeypatch.setattr(
        service, "_extract_html", lambda html, url: (url, url, url, None)
    )
    return calls


async def pending_job(db, *, url="https://example.com/article", source_id=None):
    async with db() as s:
        job = await service.create_crawl_job(
            s, target_url=url, source_id=source_id, enqueue=True
        )
        job_id = job.id
    message_id = await sql(
        db, "SELECT id FROM outbox_message WHERE aggregate_key=:key", key=str(job_id)
    )
    return job_id, message_id


@pytest.mark.parametrize("exhaust", [False, True])
async def test_busy_crawl_reconciles_same_intent_and_can_replay_dead_letter(
    db, monkeypatch, exhaust
):
    calls = fake_crawl(monkeypatch)
    job_id, message_id = await pending_job(db)
    runtime = DurableTasks(db, handlers=get_delivery_handlers(), max_attempts=2)
    async with db() as owner:
        async with crawl_source_slot(
            owner, source_id=None, url="https://example.com/feed"
        ):
            for attempt in range(2 if exhaust else 1):
                message = await runtime.claim_delivery()
                assert message["id"] == message_id
                await runtime.finish_delivery(message)
                with pytest.raises(CrawlSourceBusy, match="crawl_source_busy"):
                    await runtime.consume(message_id)
                await age_published(db)
                requeued = await runtime.reconcile()
                assert requeued == (0 if attempt == 1 else 1)
            assert calls == []
            assert (
                await sql(db, "SELECT status FROM crawl_job WHERE id=:id", id=job_id)
                == "pending"
            )
            assert (
                await sql(
                    db, "SELECT attempt_count FROM crawl_job WHERE id=:id", id=job_id
                )
                == 0
            )
            assert await sql(db, "SELECT count(*) FROM raw_artifact") == 0
            assert await sql(db, "SELECT count(*) FROM inbox_message") == 0
            assert await sql(db, "SELECT count(*) FROM outbox_message") == 1
    if exhaust:
        assert await runtime.claim_delivery() is None
        assert await runtime.consume(message_id) is False
        assert (
            await sql(db, "SELECT error_code FROM outbox_dead_letter")
            == "consumer_unacknowledged"
        )
        await runtime.replay(message_id)
    message = await runtime.claim_delivery()
    assert message["id"] == message_id
    await runtime.finish_delivery(message)
    assert await runtime.consume(message_id)
    assert await runtime.consume(message_id) is False
    assert calls == ["https://example.com/article"]
    assert (
        await sql(db, "SELECT status FROM crawl_job WHERE id=:id", id=job_id)
        == "succeeded"
    )
    assert (
        await sql(
            db, "SELECT count(*) FROM inbox_message WHERE message_id=:id", id=message_id
        )
        == 1
    )


@pytest.mark.parametrize("source_bound", [False, True])
async def test_discovery_and_crawl_share_source_admission(
    db, monkeypatch, source_bound
):
    calls = fake_crawl(monkeypatch)
    started, release = asyncio.Event(), asyncio.Event()

    class Adapter:
        async def discover(self, **kwargs):
            started.set()
            await release.wait()
            return [CollectedLink(url="https://example.com/discovered")]

    monkeypatch.setattr(service, "get_collector_adapter", lambda name: Adapter())
    async with db() as s:
        source_id = None
        if source_bound:
            source = await service.create_source(
                s, code="test", name="Test", source_type="website", base_url=None
            )
            source_id = source.id
        collector = await service.create_collector(
            s,
            code="feed",
            name="Feed",
            collector_type="rss",
            source_id=source_id,
            config={"feed_url": "https://example.com/feed"},
        )
        collector_id = collector.id
    job_id, message_id = await pending_job(db, source_id=source_id)

    async def discover():
        async with db() as s:
            return await service.run_collector_discovery(s, collector_id=collector_id)

    work = asyncio.create_task(discover())
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        with pytest.raises(CrawlSourceBusy):
            await DurableTasks(db, handlers=get_delivery_handlers()).consume(message_id)
        with pytest.raises(CrawlSourceBusy):
            await discover()
        assert calls == []
        assert await sql(db, "SELECT count(*) FROM crawl_job") == 1
        release.set()
        discovered = await asyncio.wait_for(work, timeout=5)
        assert len(discovered) == 1
        assert await sql(db, "SELECT count(*) FROM crawl_job") == 2
        assert await DurableTasks(db, handlers=get_delivery_handlers()).consume(
            message_id
        )
        assert (
            await sql(db, "SELECT status FROM crawl_job WHERE id=:id", id=job_id)
            == "succeeded"
        )
    finally:
        work.cancel()
        await asyncio.gather(work, return_exceptions=True)


async def test_inflight_worker_blocks_same_host_not_other_host_and_cancel_releases(
    db, monkeypatch
):
    calls = fake_crawl(monkeypatch)
    original_fetch = service.fetch_crawl_url
    started = asyncio.Event()
    blocked = False

    async def slow_fetch(url, **kwargs):
        nonlocal blocked
        if url.endswith("/slow") and not blocked:
            blocked = True
            started.set()
            await asyncio.Event().wait()
        return await original_fetch(url, **kwargs)

    monkeypatch.setattr(service, "fetch_crawl_url", slow_fetch)
    first_id, first_message = await pending_job(db, url="https://example.com/slow")
    _, same_message = await pending_job(db, url="https://example.com/other")
    _, other_message = await pending_job(db, url="https://another.example/other")
    runtime = DurableTasks(db, handlers=get_delivery_handlers())
    first = asyncio.create_task(runtime.consume(first_message))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        assert (
            await sql(db, "SELECT status FROM crawl_job WHERE id=:id", id=first_id)
            == "running"
        )
        with pytest.raises(CrawlSourceBusy):
            await runtime.consume(same_message)
        assert await runtime.consume(other_message)
        assert calls == ["https://another.example/other"]
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert (
            await sql(
                db,
                "SELECT count(*) FROM inbox_message WHERE message_id=:id",
                id=first_message,
            )
            == 0
        )
        assert await runtime.consume(first_message)
        assert await runtime.consume(same_message)
        assert (
            await sql(db, "SELECT status FROM crawl_job WHERE id=:id", id=first_id)
            == "succeeded"
        )
        assert calls == [
            "https://another.example/other",
            "https://example.com/slow",
            "https://example.com/other",
        ]
    finally:
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)
