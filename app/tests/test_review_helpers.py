from app.application.services.info_crawl_service import _review_metadata
from app.application.audit_context import AuditContext, reset_context, set_context


def test_review_metadata_appends_history() -> None:
    metadata = _review_metadata(
        existing={"source": "test"},
        status="reviewed",
        reviewer="alice",
        reason="looks good",
    )

    assert metadata["source"] == "test"
    assert len(metadata["review_history"]) == 1
    assert metadata["last_review"]["status"] == "reviewed"
    assert metadata["last_review"]["reviewer"] == "alice"
    assert metadata["last_review"]["reason"] == "looks good"
    assert metadata["last_review"]["reviewed_at"]
    assert metadata["last_audit"]["action"] == "review"
    assert metadata["last_audit"]["actor"] == "alice"
    assert metadata["last_audit"]["payload"]["status"] == "reviewed"


def test_review_metadata_preserves_existing_history() -> None:
    metadata = _review_metadata(
        existing={
            "review_history": [
                {
                    "status": "pending_review",
                    "reviewer": "bob",
                    "reason": "needs manual check",
                    "reviewed_at": "2026-07-06T00:00:00+00:00",
                }
            ]
        },
        status="rejected",
        reviewer=None,
        reason="duplicate",
    )

    assert len(metadata["review_history"]) == 2
    assert metadata["review_history"][0]["status"] == "pending_review"
    assert metadata["last_review"]["status"] == "rejected"
    assert metadata["last_audit"]["action"] == "review"


def test_review_metadata_carries_request_audit_context() -> None:
    token = set_context(
        AuditContext(
            correlation_id="corr-review-001",
            operation_id="op-review-001",
            reason="review fixture",
            actor_id="actor-001",
        )
    )
    try:
        metadata = _review_metadata(
            existing=None,
            status="reviewed",
            reviewer=None,
            reason="review fixture",
        )
    finally:
        reset_context(token)

    audit = metadata["last_audit"]
    assert audit["actor"] == "actor-001"
    assert audit["correlation_id"] == "corr-review-001"
    assert audit["operation_id"] == "op-review-001"
    assert audit["request_reason"] == "review fixture"
