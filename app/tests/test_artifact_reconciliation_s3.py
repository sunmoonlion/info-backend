"""Disposable versioned S3 + PostgreSQL; never use a deployed business endpoint."""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from botocore.config import Config
from test_durable_delivery_db import db as db
from test_durable_delivery_db import sql

from app.application.services import info_crawl_service as service
from app.application.services.artifact_reconciliation import PREFIX, reconcile_page
from app.infrastructure.storage.object_storage import ObjectStorage
from core.config import get_settings


@pytest.fixture
def s3(monkeypatch):
    endpoint = os.environ.get("ARTIFACT_TEST_S3_ENDPOINT")
    if not endpoint:
        pytest.skip(
            "set ARTIFACT_TEST_S3_ENDPOINT to the disposable localhost S3 server"
        )
    assert endpoint == "http://127.0.0.1:59039"
    bucket = "luna-b4-tests-" + uuid.uuid4().hex
    client = boto3.client(
        "s3",
        endpoint_url=endpoint,
        region_name="us-east-1",
        aws_access_key_id="luna-test",
        aws_secret_access_key="luna-disposable-test-only",
        config=Config(s3={"addressing_style": "path"}),
    )
    client.create_bucket(Bucket=bucket)
    client.put_bucket_versioning(
        Bucket=bucket, VersioningConfiguration={"Status": "Enabled"}
    )
    for name, value in {
        "STORAGE_BACKEND": "s3",
        "S3_ENDPOINT": endpoint,
        "S3_BUCKET": bucket,
        "S3_ACCESS_KEY_ID": "luna-test",
        "S3_SECRET_ACCESS_KEY": "luna-disposable-test-only",
        "S3_USE_TLS": "false",
        "S3_FORCE_PATH_STYLE": "true",
    }.items():
        monkeypatch.setenv(name, value)
    get_settings.cache_clear()
    try:
        yield client, bucket
    finally:
        # Only the uniquely created test bucket; production code has no DELETE.
        page = client.list_object_versions(Bucket=bucket)
        objects = [
            {"Key": obj["Key"], "VersionId": obj["VersionId"]}
            for obj in page.get("Versions", []) + page.get("DeleteMarkers", [])
        ]
        assert not page.get("IsTruncated")
        if objects:
            client.delete_objects(Bucket=bucket, Delete={"Objects": objects})
        client.delete_bucket(Bucket=bucket)
        client.close()
        get_settings.cache_clear()


async def scan(db, s3, *, mode="inventory", **kwargs):
    return await reconcile_page(db, s3[0], bucket=s3[1], mode=mode, **kwargs)


async def upload(db):
    async with db() as s:
        return await service.ingest_uploaded_file(
            s,
            filename="report.md",
            content=b"# Report\n\nOriginal body",
            content_type="text/markdown",
        )


async def test_committed_upload_reconciles_both_directions(db, s3):
    await upload(db)
    for mode in ("inventory", "references"):
        report = await scan(db, s3, mode=mode)
        assert report["counts"] == {"registered_metadata_match": 3}
        assert report["read_only"] and report["end_of_listing"]
    assert await sql(db, "SELECT count(*) FROM outbox_message") == 1


async def test_db_failure_leaves_recoverable_inventory_then_successful_retry(
    db, s3, monkeypatch
):
    original = service._enqueue_index_document_version

    async def fail(*args, **kwargs):
        raise RuntimeError("injected database transaction failure")

    monkeypatch.setattr(service, "_enqueue_index_document_version", fail)
    with pytest.raises(RuntimeError):
        await upload(db)
    assert await sql(db, "SELECT count(*) FROM raw_artifact") == 0
    assert await sql(db, "SELECT count(*) FROM info_document_version") == 0
    assert await sql(db, "SELECT count(*) FROM outbox_message") == 0
    recent = await scan(db, s3)
    assert recent["counts"] == {"unregistered_recent": 3}
    # Only the clock is advanced; no object or database mutation by reconciler.
    aged = await scan(db, s3, now=datetime.now(UTC) + timedelta(days=2), details=True)
    assert aged["counts"] == {"unregistered_candidate": 3}
    for item in aged["items"]:
        obj = s3[0].get_object(
            Bucket=s3[1], Key=item["object_key"], VersionId=item["version_id"]
        )
        assert obj["Body"].read()
        obj["Body"].close()
    monkeypatch.setattr(service, "_enqueue_index_document_version", original)
    await upload(db)
    report = await scan(db, s3)
    assert report["counts"] == {
        "unregistered_recent": 3,
        "registered_metadata_match": 3,
    }
    assert await sql(db, "SELECT count(*) FROM raw_artifact") == 3
    assert len(s3[0].list_object_versions(Bucket=s3[1])["Versions"]) == 6


async def test_successful_put_with_lost_receipt_is_still_discoverable(
    db, s3, monkeypatch
):
    original_client = ObjectStorage.s3_client

    class LostReceipt:
        def __init__(self, delegate):
            self.delegate = delegate

        def put_object(self, **kwargs):
            self.delegate.put_object(**kwargs)
            raise ConnectionError("injected response loss after server accepted PUT")

        def close(self):
            self.delegate.close()

    monkeypatch.setattr(
        ObjectStorage, "s3_client", lambda self: LostReceipt(original_client(self))
    )
    with pytest.raises(ConnectionError):
        await upload(db)
    assert await sql(db, "SELECT count(*) FROM crawl_job") == 0
    assert await sql(db, "SELECT count(*) FROM raw_artifact") == 0
    report = await scan(db, s3, details=True)
    assert report["counts"] == {"unregistered_recent": 1}
    assert report["items"][0]["version_id"]


async def test_old_version_and_delete_marker_survive_paginated_scan(db, s3):
    storage = ObjectStorage()
    key = PREFIX + "source=test/date=2026-09-12/job=probe/raw.html"
    first = storage.put_bytes(object_key=key, data=b"old", content_type="text/plain")
    second = storage.put_bytes(object_key=key, data=b"newer", content_type="text/plain")
    assert first.version_id != second.version_id
    s3[0].delete_object(Bucket=s3[1], Key=key)  # synthetic delete-marker fixture
    cursor = None
    items = []
    for _ in range(4):
        report = await scan(db, s3, limit=1, cursor=cursor, details=True)
        items.extend(report["items"])
        cursor = report["next_cursor"]
        if cursor is None:
            break
    assert cursor is None and len(items) == 3
    assert {x["state"] for x in items} == {"delete_marker", "unregistered_recent"}
    assert len(s3[0].list_object_versions(Bucket=s3[1])["Versions"]) == 2


async def test_actual_missing_version_is_reported_without_changing_reference(db, s3):
    await upload(db)
    original = await scan(db, s3, mode="references", details=True)
    victim = original["items"][0]
    s3[0].delete_object(
        Bucket=s3[1], Key=victim["object_key"], VersionId=victim["version_id"]
    )
    report = await scan(db, s3, mode="references")
    assert report["counts"] == {"registered_metadata_match": 2, "missing_version": 1}
    assert (
        await sql(
            db, "SELECT count(*) FROM raw_artifact WHERE storage_state='available'"
        )
        == 3
    )


async def test_inflight_transaction_is_reobserved_after_commit(db, s3, monkeypatch):
    staged, release = asyncio.Event(), asyncio.Event()
    original = service._enqueue_index_document_version

    async def pause(session, version_id):
        await original(session, version_id)
        staged.set()
        await asyncio.wait_for(release.wait(), 10)

    monkeypatch.setattr(service, "_enqueue_index_document_version", pause)
    writer = asyncio.create_task(upload(db))
    try:
        await asyncio.wait_for(staged.wait(), 10)
        assert (await scan(db, s3))["counts"] == {"unregistered_recent": 3}
        release.set()
        await writer
        assert (await scan(db, s3))["counts"] == {"registered_metadata_match": 3}
    finally:
        release.set()
        if not writer.done():
            writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)


async def test_real_concurrent_overwrite_does_not_change_returned_version(
    s3, monkeypatch
):
    original_client = ObjectStorage.s3_client

    class RacingWriter:
        def __init__(self, delegate):
            self.delegate = delegate

        def put_object(self, **kwargs):
            first = self.delegate.put_object(**kwargs)
            self.delegate.put_object(
                **{
                    **kwargs,
                    "Body": b"different latest bytes",
                    "Metadata": {"sha256": "f" * 64},
                }
            )
            return first

        def head_object(self, **kwargs):
            return self.delegate.head_object(**kwargs)

        def close(self):
            self.delegate.close()

    monkeypatch.setattr(
        ObjectStorage, "s3_client", lambda self: RacingWriter(original_client(self))
    )
    key = PREFIX + "concurrent/raw.html"
    stored = ObjectStorage().put_bytes(
        object_key=key, data=b"original", content_type="text/plain"
    )
    latest = s3[0].head_object(Bucket=s3[1], Key=key)
    assert stored.version_id != latest["VersionId"]
    assert stored.size_bytes == 8


async def test_suspended_versioning_fails_closed_and_object_remains_visible(db, s3):
    s3[0].put_bucket_versioning(
        Bucket=s3[1], VersioningConfiguration={"Status": "Suspended"}
    )
    with pytest.raises(RuntimeError, match="s3_version_required"):
        ObjectStorage().put_bytes(
            object_key=PREFIX + "null-version", data=b"body", content_type="text/plain"
        )
    assert (await scan(db, s3))["counts"] == {"unversioned_object": 1}
