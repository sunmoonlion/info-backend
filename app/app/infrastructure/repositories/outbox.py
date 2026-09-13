from __future__ import annotations

import uuid

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.dto.outbox import ClaimedOutboxEvent, OutboxEvent
from app.infrastructure.messaging.delivery_schedule import NOT_BEFORE_DUE_SQL


class SqlOutboxRepository:
    """PostgreSQL outbox with SKIP LOCKED leasing and explicit ownership."""

    async def enqueue(self, session: AsyncSession, event: OutboxEvent) -> uuid.UUID:
        # Frozen DTOs still contain mutable dicts; revalidate before persistence.
        event = OutboxEvent.model_validate(event.model_dump())
        message_id = uuid.uuid4()
        result = await session.execute(
            text(
                """
                INSERT INTO outbox_message (
                    id, topic, aggregate_key, deduplication_key, payload, headers,
                    available_at
                ) VALUES (
                    :id, :topic, :aggregate_key, :deduplication_key,
                    CAST(:payload AS jsonb), CAST(:headers AS jsonb),
                    COALESCE(CAST(:not_before AS timestamptz), '-infinity'::timestamptz)
                )
                ON CONFLICT (deduplication_key) DO UPDATE SET
                    deduplication_key = EXCLUDED.deduplication_key
                WHERE outbox_message.topic=EXCLUDED.topic
                  AND outbox_message.aggregate_key=EXCLUDED.aggregate_key
                  AND outbox_message.payload=EXCLUDED.payload
                  AND outbox_message.headers=EXCLUDED.headers
                RETURNING id
                """
            ),
            {
                "id": message_id,
                "topic": event.topic,
                "aggregate_key": event.aggregate_key,
                "deduplication_key": event.deduplication_key,
                "payload": self._json(event.payload),
                "headers": self._json(event.transport_headers()),
                "not_before": event.not_before,
            },
        )
        message_id = result.scalar_one_or_none()
        if message_id is None:
            raise ValueError("outbox deduplication key reused with different intent")
        return message_id

    async def claim_batch(
        self,
        session: AsyncSession,
        *,
        owner: str,
        limit: int,
        lease_seconds: int,
    ) -> list[ClaimedOutboxEvent]:
        if not owner or len(owner) > 128 or not 1 <= limit <= 1000:
            raise ValueError("invalid outbox claim parameters")
        if not 1 <= lease_seconds <= 3600:
            raise ValueError("invalid outbox lease duration")
        result = await session.execute(
            text(
                f"""
                WITH candidates AS (
                    SELECT id
                    FROM outbox_message
                    WHERE available_at <= NOW()
                      AND {NOT_BEFORE_DUE_SQL}
                      AND (
                        status = 'pending'
                        OR (
                          status = 'delivering'
                          AND lease_expires_at < NOW()
                        )
                      )
                    ORDER BY created_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT :limit
                )
                UPDATE outbox_message AS message
                SET status = 'delivering',
                    lease_owner = :owner,
                    lease_expires_at = NOW() + (:lease_seconds * INTERVAL '1 second'),
                    attempt_count = attempt_count + 1,
                    updated_at = NOW()
                FROM candidates
                WHERE message.id = candidates.id
                RETURNING message.id, message.topic, message.aggregate_key,
                          message.payload, message.headers, message.attempt_count
                """
            ),
            {"owner": owner, "limit": limit, "lease_seconds": lease_seconds},
        )
        return [
            ClaimedOutboxEvent.model_validate(dict(row)) for row in result.mappings()
        ]

    async def mark_published(
        self, session: AsyncSession, *, message_id: uuid.UUID, owner: str
    ) -> None:
        result = await session.execute(
            text(
                """
                UPDATE outbox_message
                SET status = 'published', published_at = NOW(),
                    lease_owner = NULL, lease_expires_at = NULL,
                    last_error = NULL, updated_at = NOW()
                WHERE id = :id AND status = 'delivering' AND lease_owner = :owner
                  AND lease_expires_at > clock_timestamp()
                RETURNING id
                """
            ),
            {"id": message_id, "owner": owner},
        )
        if result.scalar_one_or_none() is None:
            raise RuntimeError("outbox lease ownership was lost")

    async def mark_failed(
        self,
        session: AsyncSession,
        *,
        message_id: uuid.UUID,
        owner: str,
        error_code: str,
        retry_seconds: int,
    ) -> None:
        if not error_code or len(error_code) > 256 or not 1 <= retry_seconds <= 86400:
            raise ValueError("invalid outbox retry parameters")
        result = await session.execute(
            text(
                """
                UPDATE outbox_message
                SET status = 'pending',
                    available_at = NOW() + (:retry_seconds * INTERVAL '1 second'),
                    lease_owner = NULL, lease_expires_at = NULL,
                    last_error = :error_code, updated_at = NOW()
                WHERE id = :id AND status = 'delivering' AND lease_owner = :owner
                  AND lease_expires_at > clock_timestamp()
                RETURNING id
                """
            ),
            {
                "id": message_id,
                "owner": owner,
                "error_code": error_code,
                "retry_seconds": retry_seconds,
            },
        )
        if result.scalar_one_or_none() is None:
            raise RuntimeError("outbox lease ownership was lost")

    @staticmethod
    async def claim_inbox_once(
        session: AsyncSession, *, consumer: str, message_id: uuid.UUID
    ) -> bool:
        if not consumer or len(consumer) > 128:
            raise ValueError("invalid inbox consumer")
        result = await session.execute(
            text(
                """
                INSERT INTO inbox_message (consumer, message_id)
                VALUES (:consumer, :message_id)
                ON CONFLICT (consumer, message_id) DO NOTHING
                RETURNING message_id
                """
            ),
            {"consumer": consumer, "message_id": message_id},
        )
        return result.scalar_one_or_none() is not None

    @staticmethod
    def _json(value: object) -> str:
        import json

        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
