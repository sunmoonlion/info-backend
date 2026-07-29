"""Durable, at-least-once dispatch for Info distribution operations.

The database row is committed with the domain operation.  A broker notification
is only a wake-up signal: a lost notification is recovered by a scanner and a
duplicate notification is safe because the downstream operation has a stable
idempotency key.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.models.info import DeliveryOutboxMessage
from core.config import get_settings

logger = logging.getLogger(__name__)

TOPIC_DISTRIBUTION_DISPATCH_V1 = "info.distribution.dispatch.v1"
AGGREGATE_DISTRIBUTION = "distribution_record"

STATE_PENDING = "pending"
STATE_LEASED = "leased"
STATE_PUBLISHED = "published"
STATE_COMPLETED = "completed"


class DistributionTaskPublisher(Protocol):
    def dispatch_distribution(
        self, distribution_id: uuid.UUID, *, outbox_message_id: uuid.UUID
    ) -> str: ...


@dataclass(frozen=True)
class LeasedOutboxMessage:
    id: uuid.UUID
    distribution_id: uuid.UUID
    lease_token: uuid.UUID
    attempt_count: int


@dataclass(frozen=True)
class DispatchSummary:
    claimed: int = 0
    published: int = 0
    broker_failures: int = 0


def _now() -> datetime:
    return datetime.now(UTC)


def distribution_dispatch_idempotency_key(distribution_id: uuid.UUID) -> str:
    """Stable across scanner restarts and duplicate broker publications."""
    return f"info.distribution:{distribution_id}:dispatch-v1"


def distribution_dispatch_outbox_payload(distribution_id: uuid.UUID) -> dict[str, str]:
    """Keep broker payloads minimal and free of artifact/service credentials."""
    return {"distribution_id": str(distribution_id)}


def retry_delay_seconds(attempt_count: int) -> int:
    settings = get_settings()
    exponent = min(max(attempt_count - 1, 0), 8)
    return min(
        settings.delivery_outbox_retry_max_seconds,
        settings.delivery_outbox_retry_base_seconds * (2**exponent),
    )


def new_distribution_dispatch_outbox(
    distribution_id: uuid.UUID,
    *,
    now: datetime | None = None,
) -> DeliveryOutboxMessage:
    return DeliveryOutboxMessage(
        topic=TOPIC_DISTRIBUTION_DISPATCH_V1,
        aggregate_type=AGGREGATE_DISTRIBUTION,
        aggregate_id=distribution_id,
        idempotency_key=distribution_dispatch_idempotency_key(distribution_id),
        payload=distribution_dispatch_outbox_payload(distribution_id),
        state=STATE_PENDING,
        available_at=now or _now(),
    )


async def ensure_distribution_dispatch_outbox(
    session: AsyncSession,
    *,
    distribution_id: uuid.UUID,
    now: datetime | None = None,
) -> DeliveryOutboxMessage:
    """Create or explicitly re-arm the one durable dispatch request.

    This function deliberately does not commit.  Callers that mutate a
    DistributionRecord must use it in the same transaction as that mutation.
    """
    current_time = now or _now()
    result = await session.execute(
        select(DeliveryOutboxMessage)
        .where(
            DeliveryOutboxMessage.topic == TOPIC_DISTRIBUTION_DISPATCH_V1,
            DeliveryOutboxMessage.aggregate_type == AGGREGATE_DISTRIBUTION,
            DeliveryOutboxMessage.aggregate_id == distribution_id,
        )
        .with_for_update()
    )
    message = result.scalar_one_or_none()
    if message is None:
        message = new_distribution_dispatch_outbox(distribution_id, now=current_time)
        session.add(message)
        return message

    # Re-dispatch/retry is an explicit user action.  It cannot create a second
    # operation or idempotency key for the same DistributionRecord.
    message.state = STATE_PENDING
    message.available_at = current_time
    message.lease_token = None
    message.lease_expires_at = None
    message.published_at = None
    message.completed_at = None
    message.last_error = None
    return message


async def claim_due_delivery_outbox(
    session: AsyncSession,
    *,
    limit: int | None = None,
    now: datetime | None = None,
) -> list[LeasedOutboxMessage]:
    """Claim due work with SKIP LOCKED so multiple scanners can coexist."""
    settings = get_settings()
    current_time = now or _now()
    batch_size = limit or settings.delivery_outbox_batch_size
    published_deadline = current_time - timedelta(
        seconds=settings.delivery_outbox_ack_timeout_seconds
    )

    due = or_(
        and_(
            DeliveryOutboxMessage.state == STATE_PENDING,
            DeliveryOutboxMessage.available_at <= current_time,
        ),
        and_(
            DeliveryOutboxMessage.state == STATE_LEASED,
            or_(
                DeliveryOutboxMessage.lease_expires_at.is_(None),
                DeliveryOutboxMessage.lease_expires_at < current_time,
            ),
        ),
        and_(
            DeliveryOutboxMessage.state == STATE_PUBLISHED,
            or_(
                DeliveryOutboxMessage.published_at.is_(None),
                DeliveryOutboxMessage.published_at < published_deadline,
            ),
        ),
    )
    result = await session.execute(
        select(DeliveryOutboxMessage)
        .where(
            DeliveryOutboxMessage.topic == TOPIC_DISTRIBUTION_DISPATCH_V1,
            due,
        )
        .order_by(DeliveryOutboxMessage.available_at, DeliveryOutboxMessage.created_at)
        .with_for_update(skip_locked=True)
        .limit(batch_size)
    )
    messages = list(result.scalars())
    claims: list[LeasedOutboxMessage] = []
    for message in messages:
        lease_token = uuid.uuid4()
        message.state = STATE_LEASED
        message.lease_token = lease_token
        message.lease_expires_at = current_time + timedelta(
            seconds=settings.delivery_outbox_lease_seconds
        )
        message.attempt_count += 1
        message.available_at = current_time
        if message.published_at is not None:
            message.last_error = "worker acknowledgement timed out; republishing"
        claims.append(
            LeasedOutboxMessage(
                id=message.id,
                distribution_id=message.aggregate_id,
                lease_token=lease_token,
                attempt_count=message.attempt_count,
            )
        )
    await session.commit()
    return claims


async def mark_delivery_outbox_published(
    session: AsyncSession,
    *,
    message_id: uuid.UUID,
    lease_token: uuid.UUID,
    broker_message_id: str,
    now: datetime | None = None,
) -> bool:
    """Record broker acceptance.  A very fast worker may already complete it."""
    result = await session.execute(
        select(DeliveryOutboxMessage)
        .where(DeliveryOutboxMessage.id == message_id)
        .with_for_update()
    )
    message = result.scalar_one_or_none()
    if message is None or message.state == STATE_COMPLETED:
        await session.rollback()
        return False
    if message.state != STATE_LEASED or message.lease_token != lease_token:
        await session.rollback()
        return False
    message.state = STATE_PUBLISHED
    message.broker_message_id = broker_message_id
    message.published_at = now or _now()
    message.lease_token = None
    message.lease_expires_at = None
    message.last_error = None
    await session.commit()
    return True


async def release_delivery_outbox(
    session: AsyncSession,
    *,
    message_id: uuid.UUID,
    lease_token: uuid.UUID | None,
    error: str,
    now: datetime | None = None,
) -> bool:
    """Make a failed publication/processing attempt recoverable by the scanner."""
    result = await session.execute(
        select(DeliveryOutboxMessage)
        .where(DeliveryOutboxMessage.id == message_id)
        .with_for_update()
    )
    message = result.scalar_one_or_none()
    if message is None or message.state == STATE_COMPLETED:
        await session.rollback()
        return False
    if lease_token is not None and message.lease_token not in {lease_token, None}:
        await session.rollback()
        return False
    current_time = now or _now()
    message.state = STATE_PENDING
    message.available_at = current_time + timedelta(
        seconds=retry_delay_seconds(message.attempt_count)
    )
    message.lease_token = None
    message.lease_expires_at = None
    message.last_error = error[:4000]
    await session.commit()
    return True


async def complete_delivery_outbox(
    session: AsyncSession,
    *,
    message_id: uuid.UUID,
    now: datetime | None = None,
) -> bool:
    """Acknowledge business completion, not merely broker publication."""
    result = await session.execute(
        select(DeliveryOutboxMessage)
        .where(DeliveryOutboxMessage.id == message_id)
        .with_for_update()
    )
    message = result.scalar_one_or_none()
    if message is None:
        await session.rollback()
        return False
    if message.state == STATE_COMPLETED:
        await session.rollback()
        return True
    message.state = STATE_COMPLETED
    message.completed_at = now or _now()
    message.lease_token = None
    message.lease_expires_at = None
    message.last_error = None
    await session.commit()
    return True


async def dispatch_due_delivery_outbox(
    session: AsyncSession,
    *,
    publisher: DistributionTaskPublisher,
    limit: int | None = None,
) -> DispatchSummary:
    """Publish a batch.  Broker failure is recorded, never returned as API failure."""
    claims = await claim_due_delivery_outbox(session, limit=limit)
    published = 0
    broker_failures = 0
    for claim in claims:
        try:
            broker_message_id = publisher.dispatch_distribution(
                claim.distribution_id, outbox_message_id=claim.id
            )
        except Exception as exc:
            broker_failures += 1
            error_code = type(exc).__name__
            logger.warning(
                "outbox broker publish failed",
                extra={
                    "outbox_message_id": str(claim.id),
                    "distribution_id": str(claim.distribution_id),
                    "attempt_count": claim.attempt_count,
                    "error_code": error_code,
                },
            )
            await release_delivery_outbox(
                session,
                message_id=claim.id,
                lease_token=claim.lease_token,
                error=f"broker_publish_failed:{error_code}",
            )
            continue

        if await mark_delivery_outbox_published(
            session,
            message_id=claim.id,
            lease_token=claim.lease_token,
            broker_message_id=broker_message_id,
        ):
            published += 1
    return DispatchSummary(
        claimed=len(claims), published=published, broker_failures=broker_failures
    )
