"""Info domain handlers for the shared durable task runtime."""

import logging
import uuid

from app.infrastructure.messaging.durable_tasks import Handler

logger = logging.getLogger(__name__)


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
    from app.bootstrap.securities import (
        build_security_ingestion_service,
        enqueue_dataset_build,
    )
    from app.domain.securities import IngestionNotRunnable, IngestionStatus
    from app.infrastructure.storage.postgres import get_postgres

    ingestion_id = str(payload["ingestion_id"])
    service = build_security_ingestion_service(get_postgres().session_factory)
    try:
        summary = await service.run(ingestion_id)
    except IngestionNotRunnable:
        return
    if summary.status is IngestionStatus.SUCCEEDED:
        # 下一步和本任务的完成记录一起提交：不会漏排，也不会重复排
        await enqueue_dataset_build(session, ingestion_id)


async def build_security_dataset(session, payload):
    # 原文本身有问题（缺表、批次没成功）重投也不会变好：记日志后结束。
    # 存储、数据库的故障照常抛出，由投递机制重试。
    from app.application.securities.dataset_service import PUBLISHED
    from app.bootstrap.securities import (
        build_dataset_registration_service,
        build_security_dataset_service,
        enqueue_dataset_registration,
        record_dataset_build_refusal,
    )
    from app.domain.securities.dataset import DatasetBuildError
    from app.infrastructure.storage.postgres import get_postgres

    sessions = get_postgres().session_factory
    try:
        summary = await build_security_dataset_service(sessions).build(
            str(payload["ingestion_id"])
        )
    except DatasetBuildError as exc:
        logger.warning("security dataset build refused code=%s", exc.code)
        await record_dataset_build_refusal(
            sessions, str(payload["ingestion_id"]), exc.code
        )
        return
    if summary.status != PUBLISHED:
        return  # 质量检查没过的不发布（F-INFO-07）
    if build_dataset_registration_service(sessions).configured:
        await enqueue_dataset_registration(session, summary.record_id)


async def register_security_dataset(session, payload):
    from app.bootstrap.securities import build_dataset_registration_service
    from app.domain.securities.registration import RegistrationError
    from app.infrastructure.storage.postgres import get_postgres

    service = build_dataset_registration_service(get_postgres().session_factory)
    try:
        await service.register(str(payload["record_id"]))
    except RegistrationError as exc:
        if exc.retryable:
            raise RuntimeError(exc.code) from None
        logger.warning("security dataset registration refused code=%s", exc.code)


def get_delivery_handlers() -> dict[str, Handler]:
    return {
        "info.crawl.v1": crawl,
        "info.index.v1": index,
        "info.distribution.dispatch.v1": distribute,
        "info.security.ingest.v1": ingest_security,
        "info.security.dataset.build.v1": build_security_dataset,
        "info.security.dataset.register.v1": register_security_dataset,
    }
