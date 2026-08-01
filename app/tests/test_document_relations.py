from types import SimpleNamespace
from uuid import UUID

from app.application.services.info_crawl_service import _document_relation_metadata


def test_document_relation_metadata_preserves_review_history() -> None:
    target = SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000002"),
        canonical_url="https://example.com/canonical",
        title="Canonical Story",
    )

    metadata = _document_relation_metadata(
        existing={"review_history": [{"status": "reviewed"}]},
        target_document=target,
        relation_type="same_story",
        reviewer="alice",
        reason="same source story",
    )

    assert metadata["review_history"] == [{"status": "reviewed"}]
    assert metadata["canonical_document_id"] == str(target.id)
    assert metadata["governance_state"] == "same_story"
    assert metadata["last_document_relation"]["target_title"] == "Canonical Story"
    assert metadata["last_document_relation"]["reviewer"] == "alice"
    assert metadata["last_audit"]["action"] == "document_relation"
    assert metadata["last_audit"]["actor"] == "alice"
    assert metadata["last_audit"]["payload"]["relation_type"] == "same_story"


def test_document_relation_metadata_replaces_same_relation() -> None:
    target = SimpleNamespace(
        id=UUID("00000000-0000-0000-0000-000000000002"),
        canonical_url="https://example.com/canonical",
        title="Canonical Story",
    )

    metadata = _document_relation_metadata(
        existing={
            "document_relations": [
                {
                    "target_document_id": str(target.id),
                    "relation_type": "repost",
                    "reason": "old",
                }
            ]
        },
        target_document=target,
        relation_type="repost",
        reviewer="bob",
        reason="updated",
    )

    assert len(metadata["document_relations"]) == 1
    assert metadata["document_relations"][0]["reason"] == "updated"
    assert metadata["last_audit"]["action"] == "document_relation"
