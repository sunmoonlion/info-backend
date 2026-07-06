from app.application.services.info_crawl_service import (
    _distribution_retry_payload,
    _distribution_status_payload,
)


def test_distribution_status_payload_appends_history() -> None:
    payload = _distribution_status_payload(
        existing={"document_id": "doc-1"},
        status="failed",
        last_error="timeout",
        metadata={"remote_id": "remote-1"},
    )

    assert payload["document_id"] == "doc-1"
    assert len(payload["status_history"]) == 1
    assert payload["last_status_update"]["status"] == "failed"
    assert payload["last_status_update"]["last_error"] == "timeout"
    assert payload["last_status_update"]["metadata"]["remote_id"] == "remote-1"


def test_distribution_retry_payload_preserves_existing_history() -> None:
    payload = _distribution_retry_payload(
        {
            "retry_history": [
                {
                    "previous_error": "bad gateway",
                    "retried_at": "2026-07-06T00:00:00+00:00",
                }
            ]
        },
        previous_error="timeout",
    )

    assert len(payload["retry_history"]) == 2
    assert payload["retry_history"][0]["previous_error"] == "bad gateway"
    assert payload["last_retry"]["previous_error"] == "timeout"
