from __future__ import annotations

import asyncio
import uuid

from app.application.services.delivery_outbox import (
    complete_delivery_outbox,
    release_delivery_outbox,
)
from app.application.services.info_crawl_service import (
    dispatch_distribution as dispatch_distribution_service,
)
from app.infrastructure.storage.postgres import get_postgres
from app.worker import celery_app


@celery_app.task(name="app.tasks.dispatch_distribution")
def dispatch_distribution(distribution_id: str, outbox_message_id: str) -> str:
    """Dispatch one durable delivery request to its target app."""
    return asyncio.run(_run(uuid.UUID(distribution_id), uuid.UUID(outbox_message_id)))


async def _run(distribution_id: uuid.UUID, outbox_message_id: uuid.UUID) -> str:
    postgres = get_postgres()
    await postgres.init()
    try:
        async with postgres.session_factory() as session:
            record = await dispatch_distribution_service(
                session, distribution_id=distribution_id
            )
            # complete/release commits through the same AsyncSession and therefore
            # expires ORM instances.  Capture the stable result before that commit;
            # reading record.id/status afterwards would attempt implicit async IO
            # outside SQLAlchemy's greenlet bridge.
            record_id = record.id
            record_status = record.status
            if record_status == "succeeded":
                await complete_delivery_outbox(session, message_id=outbox_message_id)
            else:
                # dispatch_distribution_service records the domain error/pending
                # state.  Releasing the durable message makes it visible to the
                # scanner again without relying on a Celery result backend.
                await release_delivery_outbox(
                    session,
                    message_id=outbox_message_id,
                    lease_token=None,
                    error=f"distribution_{record_status}",
                )
            return str(record_id)
    finally:
        # Celery invokes the coroutine through asyncio.run(), creating a new
        # loop for every task.  Do not reuse asyncpg connections across loops.
        await postgres.shutdown()
