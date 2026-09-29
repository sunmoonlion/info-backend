from __future__ import annotations

from typing import Any, Protocol
from uuid import UUID

from app.application.dto.outbox import ClaimedOutboxEvent, OutboxEvent

# The caller's open database transaction. The port hands it to the implementation
# untouched, so the intent commits together with the caller's own state.
Transaction = Any


class OutboxRepository(Protocol):
    async def enqueue(self, session: Transaction, event: OutboxEvent) -> UUID: ...

    async def claim_batch(
        self,
        session: Transaction,
        *,
        owner: str,
        limit: int,
        lease_seconds: int,
    ) -> list[ClaimedOutboxEvent]: ...


class OutboxPublisher(Protocol):
    async def publish(self, event: ClaimedOutboxEvent) -> None: ...
