"""供给的定时的事（SDD 0045 第 3.1、3.3、3.4 节），由 beat 触发：

- 调度（每 5 秒）：把排着的作业按渠道、四档、按用户轮流排给 worker（`scheduler.py`）；
- 领待加工清单（每分钟）：knowbase 里有加工依据、还没有读法的上传资料，建加工作业；
- 原件留存到期（每小时）：交 knowbase 后留满 30 天的原件，删对象存储里的内容，记录留着；
  顺带删两天前的取数记录（限速只看近一分钟与今天）；
- 待审网站过时限（每小时）：没人审的网站，在等它的需求项转「无法满足」；待审太多、等太久
  记告警。
"""

from __future__ import annotations

import asyncio

from app.infrastructure.storage.postgres import get_postgres
from app.worker import celery_app


async def _purge() -> int:
    from app.bootstrap.supply import build_retention, build_scheduler

    postgres = get_postgres()
    await postgres.init()
    try:
        await build_scheduler(postgres.session_factory).prune()
        return await build_retention(postgres.session_factory).purge_due()
    finally:
        await postgres.shutdown()


async def _dispatch() -> int:
    from app.bootstrap.supply import build_scheduler

    postgres = get_postgres()
    await postgres.init()
    try:
        return len(await build_scheduler(postgres.session_factory).dispatch())
    finally:
        await postgres.shutdown()


async def _pull() -> int:
    from app.bootstrap.supply import build_processing_intake
    from app.infrastructure.external.knowbase_supply import (
        read_relation,
        supply_relation,
    )

    if supply_relation() is None or read_relation() is None:
        return 0  # 没接 knowbase 的两条关系（本地、测试环境）：不领，领了也读不了原件
    postgres = get_postgres()
    await postgres.init()
    try:
        return await build_processing_intake(postgres.session_factory).pull()
    finally:
        await postgres.shutdown()


async def _expire() -> int:
    from app.bootstrap.supply import build_catalog

    postgres = get_postgres()
    await postgres.init()
    try:
        return await build_catalog(postgres.session_factory).expire_sites()
    finally:
        await postgres.shutdown()


@celery_app.task(name="app.tasks.supply_upkeep.purge_originals")
def purge_originals() -> int:
    return asyncio.run(_purge())


@celery_app.task(name="app.tasks.supply_upkeep.expire_sites")
def expire_sites() -> int:
    return asyncio.run(_expire())


@celery_app.task(name="app.tasks.supply_upkeep.dispatch")
def dispatch() -> int:
    return asyncio.run(_dispatch())


@celery_app.task(name="app.tasks.supply_upkeep.pull_processing")
def pull_processing() -> int:
    return asyncio.run(_pull())
