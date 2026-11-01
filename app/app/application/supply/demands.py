"""受理需求与切合（SDD 0045 第 2.4、第四节）：精确描述与给网址。

先核什么（顺序照 0045 第 2.4 节）：
1. 同一幂等钥匙、内容相同的回原结果；同一钥匙内容不同的拒绝；
2. 格式；
3. 凭证：确认编号真、7 天内、是这个用户的，条目对得上（问 knowbase）；
4. 配额：请求频率、在途单数、每人每月新取份数（`domain/supply/quota.py`），在记录的
   同一个事务里按归属用户数，只有真正的新受理才扣；当场没有渠道的项不算新取。

受理后正式切合（0045 第四节）：按来源目录里覆盖这条描述、审过、没停用、**且满足确认时
供给条件快照**的渠道先后排，取第一个；建（或挂上已有的）取数作业，同一渠道同一描述跨
用户只取一次。第一个取不到的由作业换下一个（`jobs.py`）。没有渠道的转「无法满足」（原因
`no_channel`：这类数据我们还取不到，进缺口清单）；只有不满足供给条件的渠道能取的，转
「无法满足」附 `conditions_unmet`（当前供给条件不满足，让用户调整用途或确认新条件）。

给网址（0045 第四节「给网址」）：看网址落在哪个网站（`ensure_site`）。属于已登记、审过的
渠道照那条取；没登记的自动建一条「待审」的通用网页渠道，需求项停在「已受理」附
`site_pending`（审过以后由放行那一步排作业，`catalog.py`）；审不过、停用的转「无法满足」。
同一网址（规范化后）、同样要不要连文件的，跨用户共享一个作业。

同一应用同一幂等钥匙并发受理，后到的撞唯一索引时按已有的那张处理（独立审第 15 条）。

供给条件快照在确认里（knowbase 照存的 `supply`），knowfeed 核其中 `conditions` 一栏：
`min_license`（许可档下限）、`channels`（只用哪几条渠道），见 `domain/supply/schedule.py`。
作业排第几档按提单方定（`schedule.demand_tier`）。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import replace
from datetime import UTC, datetime

from app.application.ports.supply import Knowbase, SupplyRepository
from app.domain.supply.catalog import normalize_url, web_doc_id
from app.domain.supply.models import (
    REASON_CANCELLED,
    REASON_CATALOG_SITE,
    REASON_CHANNEL_DISABLED,
    REASON_CONDITIONS_UNMET,
    REASON_NO_CHANNEL,
    REASON_SITE_PENDING,
    REASON_SITE_REJECTED,
    Channel,
    Demand,
    DemandItem,
    DemandItemIn,
    Job,
    SupplyError,
    check_demand,
)
from app.domain.supply.quota import Quota
from app.domain.supply.schedule import Conditions, candidates, demand_tier


class DemandService:
    def __init__(
        self,
        repo: SupplyRepository,
        knowbase: Knowbase,
        *,
        quota: Quota | None = None,
        sites: Callable[[str], Awaitable[Channel]] | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self.repo = repo
        self.knowbase = knowbase
        self.quota = quota or Quota()
        # 网址落在哪条渠道（`ChannelCatalog.ensure_site`，没登记的网站自动建待审）
        self.sites = sites
        self.now = clock
        self.new_id = new_id

    async def submit(
        self,
        *,
        app: str,
        owner: str,
        proposer: str,
        chain: tuple[str, ...],
        task_ref: str | None,
        confirmation_id: str,
        idempotency_key: str,
        items: list[DemandItemIn],
        deadline: datetime | None = None,
        note: str | None = None,
        retry_of: dict[str, str] | None = None,
        consent_checked: bool = False,
    ) -> tuple[Demand, list[DemandItem], bool]:
        """受理一张需求单；返回（需求单，需求项，是不是重放）。`retry_of`：确认条目 →
        「再试一次」挂在哪一项下。`consent_checked`：订阅生成的（按主题订阅勾的候选），
        凭的是订阅范围同意，调用方已问过 knowbase 那条同意还在，这里不再核确认。"""
        check_demand(owner, proposer, idempotency_key, items)
        digest = _digest(owner, proposer, chain, task_ref, confirmation_id, items)
        prior = await self._replay(app, idempotency_key, digest)
        if prior is not None:
            return prior
        if consent_checked:
            assert proposer == "subscription"
            confirmation: dict = {
                "items": [
                    {"item_id": i.confirmation_item, "url": i.url}
                    if i.key is None
                    else {"item_id": i.confirmation_item, "key": i.key.as_dict()}
                    for i in items
                ]
            }
        else:
            confirmation = await self.knowbase.check_confirmation(
                confirmation_id, owner
            )
        confirmed = {str(i.get("item_id")): i for i in confirmation.get("items") or []}
        for item in items:
            given = confirmed.get(item.confirmation_item)
            # 给网址的确认还要核范围：连不连它直接链到的文件（二审第 10 条，knowbase 的
            # 确认条目记 `follow_links`）
            same = given is not None and (
                given.get("key") == item.key.as_dict()
                if item.key is not None
                else given.get("url") == item.url
                and bool(given.get("follow_links")) == item.follow_links
            )
            if not same:
                raise SupplyError(
                    403, "outside_confirmation", "an item is not in the confirmation"
                )
        conditions = Conditions.from_snapshot(confirmation.get("supply"))
        now = self.now()
        demand = Demand(
            id=self.new_id(),
            owner=owner,
            proposer=proposer,
            chain=chain,
            app=app,
            task_ref=task_ref,
            confirmation_id=confirmation_id,
            idempotency_key=idempotency_key,
            digest=digest,
            created_at=now,
            conditions=conditions.as_dict(),
            deadline=deadline,
            note=(note or None) and note[:500],
        )
        tier = demand_tier(proposer, app)
        channels = await self.repo.channels()
        made: list[DemandItem] = []
        jobs: dict[str, Job] = {}
        links: dict[str, str] = {}
        for given in items:
            item_id = self.new_id()
            back = (retry_of or {}).get(given.confirmation_item)
            if given.key is None:
                made.append(
                    await self._url_item(
                        item_id, demand, given, conditions, tier, jobs, links, back
                    )
                )
                continue
            covering, allowed = candidates(channels, given.key, conditions)
            if not allowed:
                made.append(
                    DemandItem(
                        id=item_id,
                        demand_id=demand.id,
                        key=given.key,
                        readings=given.readings,
                        confirmation_item=given.confirmation_item,
                        status="unsatisfiable",
                        created_at=now,
                        updated_at=now,
                        reason=REASON_CONDITIONS_UNMET
                        if covering
                        else REASON_NO_CHANNEL,
                        retry_of=back,
                    )
                )
                continue
            channel = allowed[0]
            dedup = f"fetch:{channel.id}:{given.key.text}"
            jobs.setdefault(
                dedup,
                Job(
                    id=self.new_id(),
                    kind="fetch",
                    channel_id=channel.id,
                    dedup_key=dedup,
                    payload={"key": given.key.as_dict()},
                    status="queued",
                    attempts=0,
                    created_at=now,
                    tier=tier,
                ),
            )
            links[item_id] = dedup
            made.append(
                DemandItem(
                    id=item_id,
                    demand_id=demand.id,
                    key=given.key,
                    readings=given.readings,
                    confirmation_item=given.confirmation_item,
                    status="fetching",
                    created_at=now,
                    updated_at=now,
                    channel_id=channel.id,
                    retry_of=back,
                )
            )
        try:
            await self.repo.submit(
                demand, made, list(jobs.values()), links, quota=self.quota
            )
        except SupplyError as exc:
            if exc.code != "demand_exists":
                raise
            # 同一钥匙的另一次受理先记上了：按已有的那张回（内容不同的照样拒）
            prior = await self._replay(app, idempotency_key, digest)
            if prior is None:
                raise
            return prior
        return demand, await self.repo.items_of(demand.id), False

    async def _replay(
        self, app: str, idempotency_key: str, digest: str
    ) -> tuple[Demand, list[DemandItem], bool] | None:
        prior = await self.repo.find_demand(app, idempotency_key)
        if prior is None:
            return None
        if prior.digest != digest:
            raise SupplyError(
                409, "idempotency_key_reused", "this key was used for another demand"
            )
        return prior, await self.repo.items_of(prior.id), True

    async def _url_item(
        self,
        item_id: str,
        demand: Demand,
        given: DemandItemIn,
        conditions: Conditions,
        tier: int,
        jobs: dict[str, Job],
        links: dict[str, str],
        retry_of: str | None,
    ) -> DemandItem:
        """给网址的一项：看网站审核状态（审过照取、待审等审核、审不过转「无法满足」）。"""
        if self.sites is None:
            raise SupplyError(503, "sites_not_configured", "no site catalog")
        assert given.url is not None
        url = normalize_url(given.url)
        site = await self.sites(url)
        now = demand.created_at
        base = DemandItem(
            id=item_id,
            demand_id=demand.id,
            key=None,
            readings=given.readings,
            confirmation_item=given.confirmation_item,
            status="accepted",
            created_at=now,
            updated_at=now,
            channel_id=site.id,
            url=url,
            follow_links=given.follow_links,
            prefill=dict(given.prefill),
            retry_of=retry_of,
            declared_public=given.declared_public,
        )
        if site.kind not in URL_KINDS:
            # 目录型渠道（巨潮这类）只按描述取，不按网址取：照实说，请按描述要（二审第 4 条）
            return replace(
                base,
                status="unsatisfiable",
                channel_id=None,
                reason=REASON_CATALOG_SITE,
            )
        if (
            site.review == "pending"
            and site.status != "disabled"
            and site.status_reason != "re_review"
        ):
            return replace(base, reason=REASON_SITE_PENDING)
        if (
            site.review != "approved" and site.status_reason != "re_review"
        ) or site.status == "disabled":
            reason = (
                REASON_SITE_REJECTED
                if site.review == "rejected"
                else REASON_CHANNEL_DISABLED
            )
            return replace(base, status="unsatisfiable", channel_id=None, reason=reason)
        if not conditions.allows(site):
            return replace(
                base,
                status="unsatisfiable",
                channel_id=None,
                reason=REASON_CONDITIONS_UNMET,
            )
        job = url_job(self.new_id(), site, url, given, tier, now)
        jobs.setdefault(job.dedup_key, job)
        links[item_id] = job.dedup_key
        return replace(base, status="fetching")

    async def get(self, app: str, demand_id: str) -> tuple[Demand, list[DemandItem]]:
        demand = await self.repo.get_demand(demand_id)
        if demand is None or demand.app != app:
            raise SupplyError(404, "demand_not_found", "no such demand")
        return demand, await self.repo.items_of(demand.id)

    async def cancel(self, app: str, demand_id: str) -> int:
        """取消只摘自己这张单的需求项；共享的作业照跑，别人还在等。"""
        demand, _ = await self.get(app, demand_id)
        return await self.repo.cancel_items(demand.id, self.now(), REASON_CANCELLED)

    async def mine(self, owner: str, demand_id: str) -> tuple[Demand, list[DemandItem]]:
        """本人看自己名下的一张单（不论是哪个应用代提的，0045 第 2.5 节「我的需求」）。"""
        demand = await self.repo.get_demand(demand_id)
        if demand is None or demand.owner != owner:
            raise SupplyError(404, "demand_not_found", "no such demand")
        return demand, await self.repo.items_of(demand.id)

    async def cancel_mine(self, owner: str, demand_id: str) -> int:
        demand, _ = await self.mine(owner, demand_id)
        return await self.repo.cancel_items(demand.id, self.now(), REASON_CANCELLED)


# 给网址只认这几类渠道：通用网页、订阅源、站内搜索（它们的适配器会「取网址」）
URL_KINDS = ("web", "feed", "search")


def url_job(
    job_id: str,
    site: Channel,
    url: str,
    given: DemandItemIn,
    tier: int,
    now: datetime,
) -> Job:
    """给网址的取数作业：同一网站同一网址（规范化后）、同样要不要连文件的跨用户共享。
    作业里不带谁的预填：为用户采集的，每人的预填跟着他自己的需求项交（二审第 10 条）。"""
    follow = "links" if given.follow_links else "page"
    return Job(
        id=job_id,
        kind="fetch",
        channel_id=site.id,
        dedup_key=f"fetch:{site.id}:{follow}:{web_doc_id(url)}",
        payload={"url": url, "follow_links": given.follow_links},
        status="queued",
        attempts=0,
        created_at=now,
        tier=tier,
    )


def _digest(
    owner: str,
    proposer: str,
    chain: tuple[str, ...],
    task_ref: str | None,
    confirmation_id: str,
    items: list[DemandItemIn],
) -> str:
    canonical = json.dumps(
        {
            "owner": owner,
            "proposer": proposer,
            "chain": list(chain),
            "task_ref": task_ref,
            "confirmation_id": confirmation_id,
            "items": [
                {
                    "key": i.key.as_dict() if i.key is not None else None,
                    "url": i.url,
                    "follow_links": i.follow_links,
                    "prefill": i.prefill,
                    "declared_public": i.declared_public,
                    "readings": sorted(i.readings),
                    "confirmation_item": i.confirmation_item,
                }
                for i in items
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
