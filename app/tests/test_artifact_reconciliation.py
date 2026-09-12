from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import boto3
import pytest
from botocore.exceptions import ClientError, EndpointConnectionError
from botocore.stub import Stubber
from test_durable_delivery_db import db as db
from test_durable_delivery_db import sql

from app.application.services.artifact_reconciliation import PREFIX, reconcile_page
from app.infrastructure.storage.object_storage import ObjectStorage
from core.config import get_settings

BUCKET = "luna-artifact-tests"
KEY = PREFIX + "source=test/date=2026-09-12/job=unknown/private-name.md"
HASH = hashlib.sha256(b"body").hexdigest()
NOW = datetime.now(UTC)


def client():
    return boto3.client(
        "s3",
        endpoint_url="http://127.0.0.1:1",
        aws_access_key_id="test",
        aws_secret_access_key="test",
        region_name="us-east-1",
    )


def version(version_id="v1", *, age=48):
    return {
        "Key": KEY,
        "VersionId": version_id,
        "Size": 4,
        "LastModified": NOW - timedelta(hours=age),
    }


class Inventory:
    def __init__(self, page=None, error=None):
        self.page = page or {"Versions": [version()], "IsTruncated": False}
        self.error = error
        self.calls = []

    def list_object_versions(self, **kwargs):
        self.calls.append(("list", kwargs))
        return self.page

    def head_object(self, **kwargs):
        self.calls.append(("head", kwargs))
        if self.error:
            raise self.error
        return {
            "VersionId": kwargs["VersionId"],
            "ContentLength": 4,
            "Metadata": {"sha256": HASH},
        }


async def register(db, *, version_id="v1", key=KEY):
    from app.infrastructure.models.info import CrawlJob, RawArtifact

    async with db() as s, s.begin():
        job = CrawlJob(target_url="https://example.com", status="failed")
        s.add(job)
        await s.flush()
        artifact = RawArtifact(
            crawl_job_id=job.id,
            bucket=BUCKET,
            object_key=key,
            version_id=version_id,
            sha256=HASH,
            size_bytes=4,
            content_type="text/plain",
            artifact_type="raw_html",
        )
        s.add(artifact)
        await s.flush()
        return artifact.id


async def scan(db, storage=None, **kwargs):
    return await reconcile_page(
        db,
        storage or Inventory(),
        bucket=BUCKET,
        mode=kwargs.pop("mode", "inventory"),
        now=NOW,
        **kwargs,
    )


async def test_unregistered_is_observation_not_deletion_permission(db):
    report = await scan(db)
    assert report["counts"] == {"unregistered_candidate": 1}
    assert report["read_only"] and not report["deletion_authorized"]
    assert not report["atomic_snapshot"]
    assert KEY not in json.dumps(report)
    assert await sql(db, "SELECT count(*) FROM raw_artifact") == 0
    detailed = await scan(db, details=True)
    assert detailed["items"][0]["object_key"] == KEY


async def test_registered_raw_without_document_is_not_an_orphan(db):
    artifact_id = await register(db)
    report = await scan(db)
    assert report["counts"] == {"registered_metadata_match": 1}
    assert (
        await sql(
            db,
            "SELECT document_version_id FROM raw_artifact WHERE id=:id",
            id=artifact_id,
        )
        is None
    )
    assert await sql(db, "SELECT count(*) FROM raw_artifact") == 1


async def test_recent_unregistered_and_old_version_are_distinct(db):
    await register(db, version_id="v2")
    storage = Inventory(
        {"Versions": [version("v1"), version("v2"), version("v3", age=1)]}
    )
    report = await scan(db, storage)
    assert report["counts"] == {
        "unregistered_candidate": 1,
        "registered_metadata_match": 1,
        "unregistered_recent": 1,
    }
    assert storage.calls[-1][1]["VersionId"] == "v2"


@pytest.mark.parametrize("legacy", [None, "null"])
async def test_legacy_reference_protects_every_version(db, legacy):
    await register(db, version_id=legacy)
    assert (await scan(db))["counts"] == {"protected_ambiguous_reference": 1}
    assert (await scan(db, mode="references"))["counts"] == {"unversioned_reference": 1}


async def test_extracted_content_without_version_pointer_protects_key(db):
    from test_info_identity_db import add_version, document

    from app.infrastructure.models.info import ExtractedContent

    async with db() as s, s.begin():
        v = await add_version(s, await document(s))
        s.add(
            ExtractedContent(
                document_version_id=v.id,
                content_format="text",
                bucket=BUCKET,
                object_key=KEY,
                sha256=HASH,
                size_bytes=4,
            )
        )
    assert (await scan(db))["counts"] == {"protected_ambiguous_reference": 1}


@pytest.mark.parametrize(
    "code,expected",
    [
        ("403", "storage_unavailable"),
        ("404", "missing_version"),
        ("500", "storage_unavailable"),
    ],
)
async def test_head_error_does_not_turn_into_safe_to_delete(db, code, expected):
    await register(db)
    error = ClientError(
        {"Error": {"Code": code, "Message": "private credentials"}}, "HeadObject"
    )
    report = await scan(db, Inventory(error=error), mode="references")
    assert report["counts"] == {expected: 1}
    assert "private credentials" not in json.dumps(report)
    assert not report["deletion_authorized"]


async def test_transport_failure_is_unknown_not_missing(db):
    await register(db)
    storage = Inventory(error=EndpointConnectionError(endpoint_url="https://private"))
    assert (await scan(db, storage, mode="references"))["counts"] == {
        "storage_unavailable": 1
    }


async def test_mismatched_metadata_and_no_database_mutation(db):
    await register(db)

    class BadHead(Inventory):
        def head_object(self, **kwargs):
            return {
                "VersionId": "v1",
                "ContentLength": 999,
                "Metadata": {"sha256": HASH},
            }

    assert (await scan(db, BadHead(), mode="references"))["counts"] == {
        "metadata_mismatch": 1
    }
    assert await sql(db, "SELECT storage_state FROM raw_artifact") == "available"


async def test_inventory_pagination_preserves_both_markers(db):
    first = Inventory(
        {
            "Versions": [version()],
            "IsTruncated": True,
            "NextKeyMarker": KEY,
            "NextVersionIdMarker": "v1",
        }
    )
    report = await scan(db, first, limit=1)
    assert not report["end_of_listing"]
    second = Inventory({"DeleteMarkers": [{"Key": KEY, "VersionId": "delete-v"}]})
    result = await scan(db, second, limit=1, cursor=report["next_cursor"])
    assert result["counts"] == {"delete_marker": 1}
    assert second.calls[0][1]["VersionIdMarker"] == "v1"
    assert second.calls[0][1]["KeyMarker"] == KEY
    with pytest.raises(ValueError, match="did_not_advance"):
        await scan(db, first, limit=1, cursor=report["next_cursor"])


async def test_reference_pages_are_bounded_and_restartable(db):
    ids = [await register(db, version_id=f"v{i}") for i in range(3)]
    first = await scan(db, mode="references", limit=2)
    second = await scan(db, mode="references", limit=2, cursor=first["next_cursor"])
    assert len(first["items"]) == 2 and len(second["items"]) == 1
    assert second["end_of_listing"]
    assert {x["artifact_id"] for x in first["items"] + second["items"]} == {
        str(x) for x in ids
    }


@pytest.mark.parametrize(
    "cursor",
    [
        {"mode": "references", "bucket": BUCKET, "prefix": PREFIX},
        {"mode": "inventory", "bucket": "another-domain", "prefix": PREFIX},
        {"mode": "inventory", "bucket": BUCKET, "prefix": "another/"},
    ],
)
async def test_cursor_cannot_change_domain_or_direction(db, cursor):
    with pytest.raises(ValueError, match="cursor"):
        await scan(db, cursor=cursor)


async def test_inventory_rejects_outside_prefix_and_missing_cursor(db):
    with pytest.raises(ValueError, match="outside_domain"):
        await scan(
            db, Inventory({"Versions": [{**version(), "Key": "knowledge/foreign"}]})
        )
    with pytest.raises(ValueError, match="inventory_cursor"):
        await scan(db, Inventory({"IsTruncated": True, "Versions": [version()]}))


def test_s3_put_checks_returned_version_not_latest(monkeypatch):
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    monkeypatch.setenv("S3_BUCKET", BUCKET)
    get_settings.cache_clear()
    s3 = client()
    with Stubber(s3) as stub:
        stub.add_response(
            "put_object",
            {"VersionId": "old-version"},
            {
                "Bucket": BUCKET,
                "Key": KEY,
                "Body": b"body",
                "ContentType": "text/plain",
                "Metadata": {"sha256": HASH},
            },
        )
        stub.add_response(
            "head_object",
            {
                "VersionId": "old-version",
                "ContentLength": 4,
                "Metadata": {"sha256": HASH},
            },
            {"Bucket": BUCKET, "Key": KEY, "VersionId": "old-version"},
        )
        storage = ObjectStorage()
        monkeypatch.setattr(storage, "s3_client", lambda: s3)
        try:
            assert (
                storage.put_bytes(
                    object_key=KEY, data=b"body", content_type="text/plain"
                ).version_id
                == "old-version"
            )
            stub.assert_no_pending_responses()
        finally:
            get_settings.cache_clear()


@pytest.mark.parametrize("version_id", [None, "null"])
def test_s3_unversioned_put_fails_closed(monkeypatch, version_id):
    monkeypatch.setenv("STORAGE_BACKEND", "s3")
    get_settings.cache_clear()
    s3 = client()
    with Stubber(s3) as stub:
        stub.add_response("put_object", {"VersionId": version_id} if version_id else {})
        storage = ObjectStorage()
        monkeypatch.setattr(storage, "s3_client", lambda: s3)
        try:
            with pytest.raises(RuntimeError, match="s3_version_required"):
                storage.put_bytes(
                    object_key=KEY, data=b"body", content_type="text/plain"
                )
        finally:
            get_settings.cache_clear()


@pytest.mark.parametrize(
    "counts,cursor,code",
    [
        ({"registered_metadata_match": 1}, None, 0),
        ({"registered_metadata_match": 1}, {"after_id": "more"}, 3),
        ({"unregistered_candidate": 1}, None, 2),
        ({"storage_unavailable": 1}, None, 2),
    ],
)
def test_cli_exit_codes_do_not_hide_findings_or_pagination(
    monkeypatch, capsys, counts, cursor, code
):
    from app.cli import reconcile_artifacts as cli

    async def result(args):
        return {"counts": counts, "next_cursor": cursor, "deletion_authorized": False}

    monkeypatch.setattr(cli, "run", result)
    monkeypatch.setattr("sys.argv", ["reconcile_artifacts", "--mode", "inventory"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == code
    assert json.loads(capsys.readouterr().out)["deletion_authorized"] is False


def test_cli_failure_is_incomplete_and_redacts_exception(monkeypatch, capsys):
    from app.cli import reconcile_artifacts as cli

    async def fail(args):
        raise ConnectionError("private endpoint password SQL and filenames")

    monkeypatch.setattr(cli, "run", fail)
    monkeypatch.setattr("sys.argv", ["reconcile_artifacts", "--mode", "references"])
    with pytest.raises(SystemExit) as exc:
        cli.main()
    assert exc.value.code == 1
    report = json.loads(capsys.readouterr().out)
    assert report == {
        "page_complete": False,
        "deletion_authorized": False,
        "error_type": "ConnectionError",
    }


@pytest.mark.parametrize("limit", [0, 101])
async def test_page_bounds_are_enforced_before_reading_database(db, limit):
    with pytest.raises(ValueError, match="scope"):
        await scan(db, limit=limit)
