from __future__ import annotations

import asyncio
import uuid

from app.application.services.info_crawl_service import process_crawl_job
from app.infrastructure.storage.postgres import get_postgres
from app.worker import celery_app


@celery_app.task(name="app.tasks.crawl_url")
def crawl_url(job_id: str) -> str:
    """Fetch one URL and turn it into an Info App document version."""
    return asyncio.run(_run(uuid.UUID(job_id)))


async def _run(job_id: uuid.UUID) -> str:
    postgres = get_postgres()
    await postgres.init()
    async with postgres.session_factory() as session:
        job = await process_crawl_job(session, job_id)
        return str(job.id)
