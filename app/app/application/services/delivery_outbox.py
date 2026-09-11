"""Info requests use the template outbox; the former delivery table is archival."""

import uuid

from sqlalchemy import select

from app.application.services.durable_tasks import enqueue_task
from app.infrastructure.models.info import DistributionRecord

TOPIC_DISTRIBUTION_DISPATCH_V1 = "info.distribution.dispatch.v1"


async def ensure_distribution_dispatch_outbox(session, *, distribution_id: uuid.UUID):
    record = (
        await session.execute(
            select(DistributionRecord)
            .where(DistributionRecord.id == distribution_id)
            .with_for_update()
        )
    ).scalar_one()
    generation = len((record.payload or {}).get("retry_history", []))
    key = f"info.distribution:{distribution_id}:dispatch-v1"
    if generation:
        key += f":retry:{generation}"
    return await enqueue_task(
        session,
        topic=TOPIC_DISTRIBUTION_DISPATCH_V1,
        key=str(distribution_id),
        payload={"distribution_id": str(distribution_id)},
        deduplication_key=key,
    )
