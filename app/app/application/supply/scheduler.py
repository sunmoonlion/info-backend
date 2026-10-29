"""调度（SDD 0045 第 3.3 节、第七节块 5）：按渠道排队、四档、按用户轮流、限速、日上限。

- `Scheduler.dispatch`：beat 每几秒排一轮（`tasks/supply_upkeep.py`）。每条渠道一条队列，
  按登记的「同时最多跑几个」补满；先排靠前的档，同档按用户轮流；今天份数用完的渠道这一轮
  不排。上传资料的加工排在加工队列，同时最多跑 `process_slots` 个。规矩在
  `domain/supply/schedule.py`。
- `ChannelLimiter.acquire`：插件每问一次渠道之前要一个名额（`infrastructure/supply/
  adapters.py` 的 `LimitedHttp`）。每分钟几次按渠道登记的限速、也按出站地址的全局限速
  （生产出站是家庭宽带，所有渠道挤同一个地址）；每取回一份算一份，按渠道的日上限。等
  半分钟以内就能问的原地等；更久的（日上限用完）抛 `ChannelDeferred`，作业放回队列。
  这取代了旧通用采集「按来源准入锁、不等就拒」的做法（0045 第 3.3 节）。
- `ProcessingIntake.pull`：领 knowbase 的待加工清单（关系 `user-data-supply`），每一版建一个
  加工作业（第 ④ 档，采集讨论记录第 8 条第 4 小条）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from app.application.ports.supply import Knowbase, ScheduleRepository
from app.domain.supply.models import Channel
from app.domain.supply.schedule import (
    INLINE_WAIT_SECONDS,
    ChannelDeferred,
    next_day,
)

log = logging.getLogger(__name__)

PENDING_BATCH = 50
REQUEST_LOG_DAYS = 2


class Scheduler:
    def __init__(
        self,
        repo: ScheduleRepository,
        *,
        process_slots: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repo = repo
        self.process_slots = process_slots
        self.now = clock

    async def dispatch(self) -> list[str]:
        return await self.repo.dispatch(self.now(), process_slots=self.process_slots)

    async def prune(self) -> int:
        """限速只看近一分钟与今天：两天前的取数记录删掉。"""
        return await self.repo.prune_requests(
            self.now() - timedelta(days=REQUEST_LOG_DAYS)
        )


class ChannelLimiter:
    def __init__(
        self,
        repo: ScheduleRepository,
        *,
        global_rate: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        inline_wait: float = INLINE_WAIT_SECONDS,
    ) -> None:
        self.repo = repo
        self.global_rate = global_rate
        self.now = clock
        self.sleep = sleep
        self.inline_wait = inline_wait

    async def acquire(self, channel: Channel, *, fetch: bool) -> None:
        """要一个问渠道的名额；`fetch` 是这次要取回一份（算日上限）。"""
        while True:
            now = self.now()
            later = await self.repo.take(
                channel.id,
                fetch=fetch,
                rate_per_minute=channel.rate_per_minute,
                daily_cap=channel.daily_cap,
                global_rate=self.global_rate,
                now=now,
            )
            if later is None:
                return
            wait = (later - now).total_seconds()
            if wait > self.inline_wait:
                reason = (
                    "channel_daily_cap" if later >= next_day(now) else "rate_limited"
                )
                raise ChannelDeferred(later, reason)
            await self.sleep(wait)


class ProcessingIntake:
    def __init__(
        self,
        knowbase: Knowbase,
        repo: ScheduleRepository,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.knowbase = knowbase
        self.repo = repo
        self.now = clock

    async def pull(self) -> int:
        """领一批待加工的，建加工作业；回新建了几个。"""
        pending = await self.knowbase.pending_processing(PENDING_BATCH)
        made = await self.repo.add_process_jobs(pending, self.now())
        if made:
            log.info("supply_processing_pulled pending=%s made=%s", len(pending), made)
        return made
