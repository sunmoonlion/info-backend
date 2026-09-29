"""Read-only observations, never a garbage-collection permit or second ledger.

RawArtifact remains authoritative. Inventory survives a lost PUT receipt or a
rolled-back business transaction. A live writer may commit after any observation;
unregistered_candidate is deliberately NOT named orphan or safe_to_delete.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta

from botocore.exceptions import BotoCoreError, ClientError
from sqlalchemy import select, text

from app.infrastructure.models.info import ExtractedContent, RawArtifact

PREFIX = "info/original/"
MAX_PAGE = 100


def fingerprint(bucket: str, key: str, version: str | None) -> str:
    return hashlib.sha256(json.dumps([bucket, key, version]).encode()).hexdigest()


def _entry(bucket, key, version, state, *, details, artifact_id=None):
    item = {"object_fingerprint": fingerprint(bucket, key, version), "state": state}
    if artifact_id is not None:
        item["artifact_id"] = str(artifact_id)
    if details:
        item.update(bucket=bucket, object_key=key, version_id=version)
    return item


async def _head_state(client, bucket, key, version, *, size, sha256):
    if not version or version.lower() == "null":
        return "unversioned_reference"
    try:
        head = await asyncio.to_thread(
            client.head_object, Bucket=bucket, Key=key, VersionId=version
        )
    except ClientError as exc:
        code = str(exc.response.get("Error", {}).get("Code", ""))
        if code in {"404", "NoSuchKey", "NoSuchVersion", "NotFound"}:
            return "missing_version"
        # 403 does not prove absence. Neither do transport failures or 5xx.
        return "storage_unavailable"
    except BotoCoreError:
        return "storage_unavailable"
    if (
        head.get("VersionId") != version
        or head.get("ContentLength") != size
        or head.get("Metadata", {}).get("sha256") != sha256
    ):
        return "metadata_mismatch"
    return "registered_metadata_match"


async def reconcile_page(
    sessions,
    client,
    *,
    bucket: str,
    mode: str,
    limit: int = 50,
    cursor: dict | None = None,
    details: bool = False,
    now: datetime | None = None,
) -> dict:
    """One bounded page per invocation; restart full passes to revisit live writes.

    Cursor is bound to mode and bucket. Pages are not an atomic S3/DB snapshot.
    Items redact keys by default. Continuation cursors necessarily contain S3
    key markers (possibly filenames), so keep the entire report private.
    """
    if mode not in {"inventory", "references"} or not 1 <= limit <= MAX_PAGE:
        raise ValueError("invalid_reconcile_scope")
    if cursor and (
        cursor.get("mode") != mode
        or cursor.get("bucket") != bucket
        or cursor.get("prefix") != PREFIX
    ):
        raise ValueError("invalid_reconcile_cursor")
    observed_at = now or datetime.now(UTC)
    rows = []
    next_position = None
    async with sessions() as session, session.begin():
        await session.execute(
            text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        )
        await session.execute(text("SET LOCAL statement_timeout = '5s'"))
        if mode == "references":
            query = (
                select(RawArtifact)
                .where(
                    RawArtifact.bucket == bucket,
                    RawArtifact.object_key.startswith(PREFIX),
                )
                .order_by(RawArtifact.id)
                .limit(limit + 1)
            )
            if cursor:
                query = query.where(RawArtifact.id > uuid.UUID(cursor["after_id"]))
            artifacts = list((await session.scalars(query)).all())
            if len(artifacts) > limit:
                next_position = {"after_id": str(artifacts[limit - 1].id)}
            for artifact in artifacts[:limit]:
                state = await _head_state(
                    client,
                    bucket,
                    artifact.object_key,
                    artifact.version_id,
                    size=artifact.size_bytes,
                    sha256=artifact.sha256,
                )
                item = _entry(
                    bucket,
                    artifact.object_key,
                    artifact.version_id,
                    state,
                    details=details,
                    artifact_id=artifact.id,
                )
                item["storage_state"] = artifact.storage_state
                rows.append(item)
        else:
            params = {"Bucket": bucket, "Prefix": PREFIX, "MaxKeys": limit}
            if cursor:
                key = cursor["key_marker"]
                if not isinstance(key, str) or not key.startswith(PREFIX):
                    raise ValueError("invalid_reconcile_cursor")
                params.update(KeyMarker=key, VersionIdMarker=cursor["version_marker"])
            page = await asyncio.to_thread(client.list_object_versions, **params)
            versions = page.get("Versions", [])
            markers = page.get("DeleteMarkers", [])
            if len(versions) + len(markers) > limit:
                raise ValueError("invalid_inventory_page")
            if page.get("IsTruncated"):
                key = page.get("NextKeyMarker")
                version = page.get("NextVersionIdMarker")
                if (
                    not isinstance(key, str)
                    or not key.startswith(PREFIX)
                    or not version
                ):
                    raise ValueError("invalid_inventory_cursor")
                next_position = {"key_marker": key, "version_marker": version}
                if cursor and all(cursor.get(k) == v for k, v in next_position.items()):
                    raise ValueError("inventory_cursor_did_not_advance")
            for obj in [*versions, *markers]:
                key, version = obj["Key"], obj["VersionId"]
                if not key.startswith(PREFIX):
                    raise ValueError("inventory_outside_domain")
                if obj in markers:
                    state = "delete_marker"
                elif not version or version.lower() == "null":
                    state = "unversioned_object"
                else:
                    # Every RawArtifact counts, even raw/headers with no document
                    # version (unchanged crawls and failed extraction are valid).
                    artifact = (
                        await session.scalars(
                            select(RawArtifact)
                            .where(
                                RawArtifact.bucket == bucket,
                                RawArtifact.object_key == key,
                                RawArtifact.version_id == version,
                            )
                            .limit(1)
                        )
                    ).first()
                    if artifact is not None:
                        state = await _head_state(
                            client,
                            bucket,
                            key,
                            version,
                            size=artifact.size_bytes,
                            sha256=artifact.sha256,
                        )
                    else:
                        # ExtractedContent and old version-less rows cannot prove
                        # which version they reference. Conservatively protect all.
                        legacy = await session.scalar(
                            select(RawArtifact.id)
                            .where(
                                RawArtifact.bucket == bucket,
                                RawArtifact.object_key == key,
                                (RawArtifact.version_id.is_(None))
                                | (RawArtifact.version_id == "null"),
                            )
                            .limit(1)
                        )
                        extracted = await session.scalar(
                            select(ExtractedContent.id)
                            .where(
                                ExtractedContent.bucket == bucket,
                                ExtractedContent.object_key == key,
                            )
                            .limit(1)
                        )
                        if legacy is not None or extracted is not None:
                            state = "protected_ambiguous_reference"
                        elif obj["LastModified"] > observed_at - timedelta(hours=24):
                            state = "unregistered_recent"
                        else:
                            state = "unregistered_candidate"
                rows.append(_entry(bucket, key, version, state, details=details))
        read_only = await session.scalar(text("SHOW transaction_read_only")) == "on"
    next_cursor = (
        {"mode": mode, "bucket": bucket, "prefix": PREFIX, **next_position}
        if next_position
        else None
    )
    return {
        "mode": mode,
        "observed_at": observed_at.isoformat(),
        "read_only": read_only,
        "scope": "configured_info_bucket_prefix_only",
        "atomic_snapshot": False,
        "deletion_authorized": False,
        "page_complete": True,
        "end_of_listing": next_cursor is None,
        "next_cursor": next_cursor,
        "counts": dict(Counter(item["state"] for item in rows)),
        "items": rows,
    }
