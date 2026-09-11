"""Shared Celery transport. Payloads are loaded from DB, never supplied by callers."""

from __future__ import annotations

import asyncio
import uuid

from app.application.services.durable_tasks import DurableTasks
from app.infrastructure.messaging.delivery_handlers import get_delivery_handlers
from app.infrastructure.messaging.durable_delivery import (
    DeliveryLeaseLost,
    DurableDelivery,
)
from app.infrastructure.storage.postgres import get_postgres
from app.worker import celery_app


async def pump(delivery: DurableDelivery, publish, *, limit: int = 100) -> int:
    await delivery.reconcile()
    count = 0
    for _ in range(limit):
        message = await delivery.claim_delivery()
        if message is None:
            break
        error = None
        try:
            await publish(message)
        except Exception as exc:
            error = type(exc).__name__  # Do not persist broker URLs or credentials.
        try:
            await delivery.finish_delivery(message, error=error)
        except DeliveryLeaseLost:
            pass  # A later owner is authoritative; do not overwrite its result.
        count += 1
    return count


async def _run(message_id: uuid.UUID | None):
    postgres = get_postgres()
    await postgres.init()
    try:
        delivery = DurableTasks(
            postgres.session_factory, handlers=get_delivery_handlers()
        )
        if message_id is not None:
            return await delivery.consume(message_id)

        async def publish(message):
            await asyncio.to_thread(
                execute.apply_async,
                args=[str(message["id"])],
                task_id=str(message["id"]),
            )

        return await pump(delivery, publish)
    finally:
        await postgres.shutdown()


@celery_app.task(name="app.tasks.durable_delivery.pump")
def dispatch():
    return asyncio.run(_run(None))


@celery_app.task(name="app.tasks.durable_delivery.execute")
def execute(message_id: str):
    return asyncio.run(_run(uuid.UUID(message_id)))
