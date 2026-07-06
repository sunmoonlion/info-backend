from app.application.services.info_crawl_service import _review_metadata


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
