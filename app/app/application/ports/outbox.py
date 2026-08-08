from __future__ import annotations

from typing import Protocol
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.dto.outbox import ClaimedOutboxEvent, OutboxEvent


class OutboxRepository(Protocol):
    async def enqueue(self, session: AsyncSession, event: OutboxEvent) -> UUID: ...

    async def claim_batch(
        self,
        session: AsyncSession,
        *,
        owner: str,
        limit: int,
        lease_seconds: int,
    ) -> list[ClaimedOutboxEvent]: ...


class OutboxPublisher(Protocol):
    async def publish(self, event: ClaimedOutboxEvent) -> None: ...
