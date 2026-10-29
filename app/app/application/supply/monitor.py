"""管理端看的两样（SDD 0045 第一节、第四节、第 3.3 节）：缺口清单与队列监控。

**缺口清单**：需求项「无法满足」的汇总，给平台人员看要补哪些渠道。按「哪种无法满足 ×
类型」分组：`uncovered` 是「这类数据我们还取不到」（来源目录里没有能供的渠道，或供它的
渠道停用了），是补渠道的主要依据；`absent`（渠道里没有这一份）、`site`（网站审不过或
没人审）一并列出。每组写几项、几个人要过、几个主体、最早最近一次，看不出是谁要的。

**队列监控**：各渠道的作业排着几个、在跑几个、等重试几个、一天里成了几个败了几个、
最早那个排了多久，最近失败的原因；可以看一个作业的每次尝试，手动把等着的作业马上再排。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.application.ports.supply import MonitorRepository
from app.domain.supply.models import SupplyError

DAY = timedelta(days=1)
GAP_KINDS = ("uncovered", "absent", "site")


class SupplyMonitor:
    def __init__(
        self,
        repo: MonitorRepository,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repo = repo
        self.now = clock

    async def gaps(self, kind: str | None = None) -> list[dict[str, object]]:
        if kind is not None and kind not in GAP_KINDS:
            raise SupplyError(422, "gap_kind_invalid", "kind is uncovered/absent/site")
        return await self.repo.gaps(kind)

    async def queue(self) -> dict[str, object]:
        return await self.repo.queue(self.now() - DAY)

    async def jobs(
        self, *, channel_id: str | None, status: str | None, limit: int
    ) -> list[dict[str, object]]:
        return await self.repo.jobs(
            channel_id=channel_id, status=status, limit=max(1, min(limit, 200))
        )

    async def job(self, job_id: str) -> dict[str, object]:
        found = await self.repo.job(job_id)
        if found is None:
            raise SupplyError(404, "job_not_found", "no such job")
        return found

    async def requeue(self, job_id: str) -> dict[str, object]:
        """等着的作业（排队中、等重试）马上再排一次；渠道没在用的不排。"""
        found = await self.job(job_id)
        if found["status"] not in ("queued", "waiting"):
            raise SupplyError(409, "job_not_open", "only queued or waiting jobs")
        if found["channel_status"] != "active":
            raise SupplyError(409, "channel_not_active", "resume the channel first")
        await self.repo.requeue(job_id)
        return await self.job(job_id)
