"""预切合（SDD 0045 第四节、契约 `knowfeed-supply-prematch/v1`，模型在 `domain/supply/prematch.py`）。

确认之前回答应用：这几项这类能不能取、网站审没审过、候选渠道的许可档、按队列与限速估
的预计时间。**只查来源目录与队列**：不问渠道、不看 knowbase 已有什么，不建待审网站
（没登记的网站回 `site_unregistered`，真提了需求才自动建待审）。

- 描述：来源目录里能供这条描述的渠道按先后排（同受理时的切合，`schedule.candidates`），
  满足给的条件（许可档下限、只用哪几条渠道）的列为候选；没有能供的回「取不到」
  （`no_channel`），能供的都不满足条件回 `conditions_unmet`。主体现在只认 A 股证券代码
  （同受理时的格式核验）。
- 网址：看落在哪个网站。已登记的网站照那条渠道：审过的能取、待审的「网站待审」、审不过
  或停用的「取不到」。
- 预计时间按调度的档次算：knowlab 提的第 ② 档，其余第 ① 档（`schedule.demand_tier`）。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import UTC, datetime

from app.application.ports.supply import (
    CatalogRepository,
    ScheduleRepository,
    SupplyRepository,
)
from app.domain.supply.catalog import site_of
from app.domain.supply.models import (
    REASON_NO_CHANNEL,
    REASON_SITE_REJECTED,
    Channel,
    DescriptionKey,
)
from app.domain.supply.prematch import (
    CandidateV1,
    PrematchAnswerV1,
    PrematchItemV1,
    PrematchRequestV1,
)
from app.domain.supply.schedule import (
    Conditions,
    candidates,
    demand_tier,
    estimate_seconds,
)

_SUBJECT = re.compile(r"^\d{6}\.(SH|SZ|BJ)$")


class PreMatch:
    def __init__(
        self,
        repo: SupplyRepository,
        catalog: CatalogRepository,
        schedule: ScheduleRepository,
        *,
        global_rate: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repo = repo
        self.catalog = catalog
        self.schedule = schedule
        self.global_rate = global_rate
        self.now = clock

    async def check(
        self, app: str, request: PrematchRequestV1
    ) -> list[PrematchAnswerV1]:
        conditions = Conditions.from_dict(request.conditions.model_dump())
        channels = await self.repo.channels()
        ahead = await self.schedule.queue_ahead(demand_tier("app", app))
        fetched = await self.schedule.fetched_today(self.now())

        def eta(channel: Channel) -> int | None:
            return estimate_seconds(
                channel,
                ahead=ahead.get(channel.id, 0),
                fetched_today=fetched.get(channel.id, 0),
                global_rate=self.global_rate,
                now=self.now(),
            )

        answers = []
        for item in request.items:
            if (item.key is None) == (item.url is None):
                answers.append(
                    PrematchAnswerV1(
                        ref=item.ref, status="unavailable", reason="item_invalid"
                    )
                )
            elif item.key is not None:
                answers.append(self._by_key(item, channels, conditions, eta))
            else:
                answers.append(await self._by_url(item, conditions, eta))
        return answers

    def _by_key(
        self,
        item: PrematchItemV1,
        channels: list[Channel],
        conditions: Conditions,
        eta: Callable[[Channel], int | None],
    ) -> PrematchAnswerV1:
        assert item.key is not None
        if not _SUBJECT.fullmatch(item.key.subject):
            return PrematchAnswerV1(
                ref=item.ref, status="unavailable", reason="subject_unsupported"
            )
        key = DescriptionKey(
            item.key.subject,
            item.key.doc_type,
            item.key.period_start,
            item.key.period_end,
            edition=item.key.edition,
            topics=tuple(item.key.topics),
            industry=item.key.industry,
        )
        covering, allowed = candidates(channels, key, conditions)
        if not covering:
            return PrematchAnswerV1(
                ref=item.ref, status="unavailable", reason=REASON_NO_CHANNEL
            )
        if not allowed:
            return PrematchAnswerV1(ref=item.ref, status="conditions_unmet")
        return _available(item, allowed, eta)

    async def _by_url(
        self,
        item: PrematchItemV1,
        conditions: Conditions,
        eta: Callable[[Channel], int | None],
    ) -> PrematchAnswerV1:
        assert item.url is not None
        host = site_of(item.url)
        channel = await self.catalog.channel_for_host(host)
        if channel is None:
            return PrematchAnswerV1(
                ref=item.ref, status="site_pending", reason="site_unregistered"
            )
        if channel.review == "pending":
            return PrematchAnswerV1(
                ref=item.ref, status="site_pending", reason="site_pending"
            )
        if channel.review != "approved" or channel.status == "disabled":
            return PrematchAnswerV1(
                ref=item.ref, status="unavailable", reason=REASON_SITE_REJECTED
            )
        if not conditions.allows(channel):
            return PrematchAnswerV1(ref=item.ref, status="conditions_unmet")
        return _available(item, [channel], eta)


def _available(
    item: PrematchItemV1,
    allowed: list[Channel],
    eta: Callable[[Channel], int | None],
) -> PrematchAnswerV1:
    found = [
        CandidateV1(
            channel_id=c.id,
            name=c.name,
            license=c.license,  # type: ignore[arg-type]
            status=c.status,  # type: ignore[arg-type]
            readings=list(c.readings),  # type: ignore[arg-type]
            eta_seconds=eta(c),
        )
        for c in allowed
    ]
    first = found[0]
    return PrematchAnswerV1(
        ref=item.ref,
        status="available",
        license=first.license,
        eta_seconds=first.eta_seconds,
        readings_missing=[r for r in item.readings if r not in first.readings],
        candidates=found,
    )
