from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest

from app.application.services.info_crawl_service import (
    _apply_duplicate_metadata,
    _content_fingerprint,
    _hash_text,
)


def test_content_fingerprint_builds_stable_simhash() -> None:
    first = _content_fingerprint(
        "Markets rallied after policy makers signaled a softer inflation path."
    )
    second = _content_fingerprint(
        "Markets rallied after policymakers signaled softer inflation trends."
    )

    assert first["algorithm"] == "simhash64"
    assert first["value"]
    assert second["value"]
    assert first["token_count"] > 0


@pytest.mark.asyncio
async def test_apply_duplicate_metadata_records_exact_and_near_candidates() -> None:
    same_text = "Central bank officials discussed inflation and liquidity support."
    near_text = "Central bank officials discuss inflation and market liquidity support."
    current = SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        metadata_json={"review_history": [{"status": "reviewed"}]},
    )
    exact = SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000002"),
        canonical_url="https://example.com/exact",
        title="Exact",
        content_hash=_hash_text(same_text),
        metadata_json={},
    )
    near = SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000003"),
        canonical_url="https://example.com/near",
        title="Near",
        content_hash="different",
        metadata_json={"content_fingerprint": _content_fingerprint(near_text)},
    )

    class Result:
        def scalars(self) -> list[Any]:
            return [exact, near]

    class Session:
        async def execute(self, _statement: object) -> Result:
            return Result()

    await _apply_duplicate_metadata(
        session=cast(Any, Session()),
        document=cast(Any, current),
        content_hash=_hash_text(same_text),
        text=same_text,
    )

    assert current.metadata_json["review_history"] == [{"status": "reviewed"}]
    assert current.metadata_json["duplicate_state"] == "exact_duplicate"
    assert current.metadata_json["content_fingerprint"]["algorithm"] == "simhash64"
    assert current.metadata_json["duplicate_candidates"][0]["match_type"] == "exact_hash"
    assert current.metadata_json["duplicate_candidates"][1]["match_type"] == "simhash64"
