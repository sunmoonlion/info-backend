from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.application.dto.outbox import OutboxEvent


def test_outbox_event_has_bounded_transport_contract() -> None:
    event = OutboxEvent(
        topic="knowledge.ingestion.requested.v1",
        aggregate_key="document-version-1",
        deduplication_key="distribution-1:knowledge",
        payload={"artifact_id": "artifact-1"},
        headers={"contract_version": "1"},
    )
    assert event.topic.endswith(".v1")
    with pytest.raises(ValidationError, match="256 KiB"):
        OutboxEvent(
            topic="oversized.v1",
            aggregate_key="record-1",
            deduplication_key="record-1",
            payload={"data": "x" * 262_144},
        )
