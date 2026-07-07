from app.application.services.info_crawl_service import _document_summary_metadata


def test_document_summary_metadata_normalizes_tags_and_preserves_existing_metadata() -> None:
    metadata = _document_summary_metadata(
        existing={"entity_links": {"topics": ["AI"]}},
        summary="  Concise market update.  ",
        tags=["Markets", " markets ", "Policy"],
        importance_score=0.8,
        importance_reason="market-moving policy signal",
        reviewer="alice",
        reason="manual summary",
    )

    assert metadata["entity_links"] == {"topics": ["AI"]}
    assert metadata["summary_profile"]["summary"] == "Concise market update."
    assert metadata["summary_profile"]["tags"] == ["Markets", "Policy"]
    assert metadata["summary_profile"]["importance_score"] == 0.8
    assert metadata["summary_profile"]["importance_reason"] == "market-moving policy signal"
    assert metadata["last_summary_update"]["reviewer"] == "alice"
    assert len(metadata["summary_history"]) == 1


def test_document_summary_metadata_appends_history() -> None:
    metadata = _document_summary_metadata(
        existing={"summary_history": [{"reason": "old"}]},
        summary=None,
        tags=[],
        importance_score=None,
        importance_reason=None,
        reviewer=None,
        reason="clear profile",
    )

    assert metadata["summary_profile"]["summary"] is None
    assert metadata["summary_profile"]["tags"] == []
    assert len(metadata["summary_history"]) == 2
    assert metadata["summary_history"][-1]["reason"] == "clear profile"
