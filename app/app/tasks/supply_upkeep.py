"""供给的两件定时的事（SDD 0045 第 3.1、3.4 节），由 beat 每小时触发一次：

- 原件留存到期：交 knowbase 后留满 30 天的原件，删对象存储里的内容，记录留着；
- 待审网站过时限：没人审的网站，在等它的需求项转「无法满足」；待审太多、等太久记告警。
"""

from __future__ import annotations

import asyncio

from app.infrastructure.storage.postgres import get_postgres
from app.worker import celery_app


async def _purge() -> int:
    from app.bootstrap.supply import build_retention

    postgres = get_postgres()
    await postgres.init()
    try:
        return await build_retention(postgres.session_factory).purge_due()
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
