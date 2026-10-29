"""Info domain handlers for the shared durable task runtime."""

import uuid

from app.infrastructure.messaging.durable_tasks import Handler


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


async def run_supply_job(session, payload):
    # 作业自己管事务、尝试与重试（JobRunner）：业务上的失败是需求项的终态，不是投递失败
    from app.bootstrap.supply import build_job_runner
    from app.infrastructure.storage.postgres import get_postgres

    await build_job_runner(get_postgres().session_factory).run(str(payload["job_id"]))


async def refresh_security_directory(session, payload):
    # 证券名录（查主体用）：取巨潮的证券列表整份换；取不到抛错，由投递重试
    from app.bootstrap.supply import build_directory_refresh

    await build_directory_refresh()


async def run_channel_trial(session, payload):
    # 试采（0045 第 3.1 节）：要出公网，只在 worker 跑；结果记在试采记录里给管理端看
    from app.bootstrap.supply import build_trial_runner
    from app.infrastructure.storage.postgres import get_postgres

    await build_trial_runner(get_postgres().session_factory).run(
        str(payload["trial_id"])
    )


async def notify_demand_status(session, payload):
    # 需求状态推送（0045 第 2.5 节）：交给提单的应用；对方没收下抛错，由投递重试
    from app.bootstrap.supply import build_notifier

    await build_notifier().deliver(payload)


def get_delivery_handlers() -> dict[str, Handler]:
    return {
        "knowfeed.crawl.v1": crawl,
        "knowfeed.index.v1": index,
        "knowfeed.distribution.dispatch.v1": distribute,
        "knowfeed.supply.job.v1": run_supply_job,
        "knowfeed.supply.directory.refresh.v1": refresh_security_directory,
        "knowfeed.supply.channel.trial.v1": run_channel_trial,
        "knowfeed.supply.demand.status.v1": notify_demand_status,
    }
