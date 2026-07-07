from __future__ import annotations

import asyncio
import uuid

from app.application.services.info_crawl_service import (
    dispatch_distribution as dispatch_distribution_service,
)
from app.infrastructure.storage.postgres import get_postgres
from app.worker import celery_app


@celery_app.task(name="app.tasks.dispatch_distribution")
def dispatch_distribution(distribution_id: str) -> str:
    """Dispatch one distribution_record to its target app."""
    return asyncio.run(_run(uuid.UUID(distribution_id)))


async def _run(distribution_id: uuid.UUID) -> str:
    postgres = get_postgres()
    await postgres.init()
    async with postgres.session_factory() as session:
        record = await dispatch_distribution_service(session, distribution_id=distribution_id)
        return str(record.id)
