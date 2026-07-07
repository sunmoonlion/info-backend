from app.application.services.info_crawl_service import _document_entity_metadata


def test_document_entity_metadata_normalizes_and_preserves_existing_metadata() -> None:
    metadata = _document_entity_metadata(
        existing={"review_history": [{"status": "reviewed"}]},
        companies=["Apple", " apple ", "Tesla"],
        securities=["AAPL", "", "TSLA"],
        industries=["Technology", "technology"],
        topics=["AI", "EV", "AI"],
        reviewer="alice",
        reason="manual tagging",
    )

    assert metadata["review_history"] == [{"status": "reviewed"}]
    assert metadata["entity_links"]["companies"] == ["Apple", "Tesla"]
    assert metadata["entity_links"]["securities"] == ["AAPL", "TSLA"]
    assert metadata["entity_links"]["industries"] == ["Technology"]
    assert metadata["entity_links"]["topics"] == ["AI", "EV"]
    assert metadata["last_entity_link_update"]["reviewer"] == "alice"
    assert len(metadata["entity_link_history"]) == 1


def test_document_entity_metadata_appends_history() -> None:
    metadata = _document_entity_metadata(
        existing={"entity_link_history": [{"reason": "old"}]},
        companies=[],
        securities=[],
        industries=[],
        topics=["Policy"],
        reviewer=None,
        reason="updated",
    )

    assert metadata["entity_links"]["topics"] == ["Policy"]
    assert len(metadata["entity_link_history"]) == 2
    assert metadata["entity_link_history"][-1]["reason"] == "updated"
