"""Frozen Info identity policy v1, also used by migration 0008.

Do not change this policy in place: a new equivalence relation needs a new
policy/migration and collision audit. canonical_url itself remains untouched.
"""

from __future__ import annotations

import hashlib
import ipaddress
from collections.abc import Iterable
from urllib.parse import urlsplit, urlunsplit

import httpx

MAX_PREFLIGHT_ROWS = 100_000


class IdentityURLInvalid(ValueError):
    pass


def normalize_url_v1(value: str) -> str:
    # Preserve the historical, opaque upload identity (including filename case,
    # spaces and #). Workspace/user-scoped upload identity is a separate change.
    if value.startswith("upload://") and len(value) > len("upload://"):
        if "\x00" in value:
            raise IdentityURLInvalid("identity_url_invalid")
        return value
    if any(ord(c) <= 32 or ord(c) == 127 for c in value) or "\\" in value:
        raise IdentityURLInvalid("identity_url_invalid")
    try:
        parts = urlsplit(value)
        parsed = httpx.URL(value)
        if (
            parts.scheme not in {"http", "https"}
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or "%" in parsed.host
        ):
            raise ValueError()
        host = parsed.raw_host.decode("ascii").lower()
        try:
            host = str(ipaddress.ip_address(host))
        except ValueError:
            pass
        if ":" in host:
            host = "[" + host + "]"
        port = parts.port
        if port is not None and not 1 <= port <= 65535:
            raise ValueError()
        if port is not None and port != (443 if parts.scheme == "https" else 80):
            host += ":" + str(port)
        result = urlunsplit((parts.scheme, host, parts.path or "/", parts.query, ""))
        # An explicit empty query can be significant to an origin; preserve it.
        if "?" in value.split("#", 1)[0] and not parts.query:
            result += "?"
        return result
    except (ValueError, httpx.InvalidURL, UnicodeError):
        raise IdentityURLInvalid("identity_url_invalid") from None


def identity_key_v1(url: str | None) -> str | None:
    if url is None:
        return None
    normalized = normalize_url_v1(url)
    try:
        return hashlib.sha256(
            ("info.canonical.v1\x00" + normalized).encode()
        ).hexdigest()
    except UnicodeError:
        raise IdentityURLInvalid("identity_url_invalid") from None


def audit_identity_rows(rows: Iterable[tuple[object, str | None]]) -> dict:
    """Bounded audit. Only counts and document IDs, never URLs or query tokens."""
    seen: dict[str, list[str]] = {}
    count = anonymous = changed = invalid = 0
    invalid_ids: list[str] = []
    for document_id, url in rows:
        count += 1
        if count > MAX_PREFLIGHT_ROWS:
            return {
                "ready": False,
                "complete": False,
                "error": "identity_audit_capacity_exceeded",
            }
        if url is None:
            anonymous += 1
            continue
        try:
            key = identity_key_v1(url)
            changed += int(normalize_url_v1(url) != url)
        except IdentityURLInvalid:
            invalid += 1
            if len(invalid_ids) < 10:
                invalid_ids.append(str(document_id))
            continue
        assert key is not None
        seen.setdefault(key, []).append(str(document_id))
    duplicates = [ids for ids in seen.values() if len(ids) > 1]
    return {
        "ready": not invalid and not duplicates,
        "complete": True,
        "policy": "info.canonical.v1",
        "rows": count,
        "null_urls": anonymous,
        "normalization_changes": changed,
        "invalid_urls": invalid,
        "invalid_document_ids_sample": invalid_ids,
        "duplicate_groups": len(duplicates),
        "duplicate_documents": sum(map(len, duplicates)),
        "duplicate_document_ids_sample": [ids[:10] for ids in duplicates[:10]],
    }
