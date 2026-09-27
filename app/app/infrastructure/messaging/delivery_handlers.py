"""Info domain handlers for the shared durable task runtime."""

import uuid

from app.application.services.durable_tasks import Handler


async def crawl(session, payload):
    from app.application.services.info_crawl_service import process_crawl_job

    await process_crawl_job(session, uuid.UUID(payload["job_id"]))


async def index(session, payload):
    from app.application.services.info_crawl_service import index_document_version

    result = await index_document_version(
        session, document_version_id=uuid.UUID(payload["document_version_id"])
    )
    if result["failed"]:
        raise RuntimeError("search_index_failed")


async def distribute(session, payload):
    from app.application.services.info_crawl_service import dispatch_distribution

    record = await dispatch_distribution(
        session, distribution_id=uuid.UUID(payload["distribution_id"])
    )
    if record.status != "succeeded":
        raise RuntimeError("distribution_not_acknowledged")


async def ingest_security(session, payload):
    # 批次自己管事务与状态：采集失败是批次的终态，不是投递失败，不重投。
    # 批次不在待执行状态（重复投递、上次被打断）时什么也不做。
    from app.bootstrap.securities import build_security_ingestion_service
    from app.domain.securities import IngestionNotRunnable
    from app.infrastructure.storage.postgres import get_postgres

    service = build_security_ingestion_service(get_postgres().session_factory)
    try:
        await service.run(str(payload["ingestion_id"]))
    except IngestionNotRunnable:
        return


def get_delivery_handlers() -> dict[str, Handler]:
    return {
        "info.crawl.v1": crawl,
        "info.index.v1": index,
        "info.distribution.dispatch.v1": distribute,
        "info.security.ingest.v1": ingest_security,
    }
