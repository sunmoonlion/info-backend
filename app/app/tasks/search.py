from __future__ import annotations

import asyncio
import uuid

from app.application.services.info_crawl_service import (
    index_document_version as index_document_version_service,
)
from app.infrastructure.storage.postgres import get_postgres
from app.worker import celery_app


@celery_app.task(name="app.tasks.index_document_version")
def index_document_version(document_version_id: str) -> dict:
    """Index one document_version into the rebuildable Info App search model."""
    return asyncio.run(_run(uuid.UUID(document_version_id)))


async def _run(document_version_id: uuid.UUID) -> dict:
    postgres = get_postgres()
    await postgres.init()
    async with postgres.session_factory() as session:
        return await index_document_version_service(
            session, document_version_id=document_version_id
        )
