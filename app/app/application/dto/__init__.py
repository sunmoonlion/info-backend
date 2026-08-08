"""Application-owned transport-neutral data contracts."""
from app.application.dto.outbox import ClaimedOutboxEvent, OutboxEvent

__all__ = ["ClaimedOutboxEvent", "OutboxEvent"]
