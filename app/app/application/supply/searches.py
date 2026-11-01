"""按主题找（SDD 0045 第 2.2、2.5 节，第四节「按主题找」；发版前补齐 F1、F4）。

用户只给话题，先列出候选，用户勾了再确认、再去取。**第一版只在用户指定的网站与审核过的
站内搜索渠道里搜**（所有者 10-26 定，通用搜索接口等律师意见）：每条审过、在用的搜索型渠道
排一个「搜」作业（出公网，在 worker 里跑），结果按渠道先后、渠道给的原始顺序列，不重排、
不评级。每条候选先预切合：标由哪个渠道取（网址落在哪个网站）、许可哪一档、网站审没审过。

一生：正在找 → 候选已出（7 天）→ 已结束（勾过、作废）。按主题订阅的每一轮也是一次搜，勾的
凭订阅的范围同意，不另记确认（0045 第 2.3 节）。勾的每条变成一个给网址的需求项。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from app.application.ports.supply import (
    CatalogRepository,
    Knowbase,
    KnowbaseUser,
    SupplyRepository,
)
from app.application.supply.demands import DemandService
from app.application.supply.jobs import Ask
from app.domain.supply.catalog import on_site, site_of
from app.domain.supply.models import (
    Channel,
    Demand,
    DemandItem,
    DemandItemIn,
    Job,
    SupplyError,
    refused,
)
from app.domain.supply.quota import BEIJING
from app.domain.supply.schedule import TIER_SUBSCRIPTION, TIER_TASK
from app.domain.supply.subscription import (
    CANDIDATES_VALID,
    Search,
    Subscription,
    check_search,
)

# 拟值：每人每小时最多找几次、同时最多在找几个；同一渠道同一话题多久以内复用结果
SEARCHES_PER_HOUR = 20
OPEN_SEARCHES = 3
REUSE_WITHIN = timedelta(minutes=10)


class SearchService:
    def __init__(
        self,
        repo: Any,
        supply: SupplyRepository,
        demands: DemandService,
        knowbase: Knowbase,
        user: KnowbaseUser | None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self.repo = repo
        self.supply = supply
        self.demands = demands
        self.knowbase = knowbase
        self.user = user
        self.now = clock
        self.new_id = new_id

    async def start(
        self,
        owner: str,
        topic: str,
        sites: tuple[str, ...],
        wanted: int,
        *,
        subscription: Subscription | None = None,
    ) -> Search:
        cleaned = check_search(topic, sites, wanted)
        now = self.now()
        if subscription is None:
            # 频率配额（0045 第 2.4 节）：按用户每小时几次、同时在找几个（拟值）
            if await self.repo.count_searches(owner, now - timedelta(hours=1)) >= (
                SEARCHES_PER_HOUR
            ):
                raise SupplyError(
                    429, "quota_search_rate", "too many searches this hour"
                )
            if await self.repo.open_searches(owner) >= OPEN_SEARCHES:
                raise SupplyError(
                    429, "quota_open_searches", "too many searches running"
                )
        channels = [c for c in await self.supply.channels() if _searchable(c)]
        if cleaned:
            channels = [
                c
                for c in channels
                if c.domain
                and any(on_site(s, c.domain) or on_site(c.domain, s) for s in cleaned)
            ]
        # 同一渠道同一话题短时间里搜过的，直接用那次的结果（0045 第 3.3 节「同样话题短时
        # 缓存」），不再问渠道
        reused: list[dict[str, object]] = []
        to_ask: list[Channel] = []
        for c in channels:
            found = await self.repo.recent_candidates(
                c.id, topic.strip(), now - REUSE_WITHIN
            )
            if found is None:
                to_ask.append(c)
            else:
                reused += [{k: v for k, v in x.items() if k != "ref"} for x in found][
                    :wanted
                ]
        status = "searching" if to_ask else ("ready" if channels else "ended")
        search = Search(
            id=self.new_id(),
            owner=owner,
            topic=topic.strip(),
            sites=cleaned,
            wanted=wanted,
            status=status,
            created_at=now,
            subscription_id=subscription.id if subscription else None,
            expires_at=now + CANDIDATES_VALID if status == "ready" else None,
            # 没有审过的站内搜索（或用户指定的网站没有）：照实说，不去别处搜
            note=None if channels else "no_search_channel",
        )
        jobs = [
            Job(
                id=self.new_id(),
                kind="search",
                channel_id=c.id,
                dedup_key=f"search:{search.id}:{c.id}",
                payload={
                    "search_id": search.id,
                    "owner": owner,
                    "topic": search.topic,
                    "wanted": wanted,
                },
                status="queued",
                attempts=0,
                created_at=now,
                tier=TIER_SUBSCRIPTION if subscription else TIER_TASK,
            )
            for c in to_ask
        ]
        await self.repo.add_search(search, jobs, reused)
        return await self.repo.get_search(search.id) or search

    async def get(self, owner: str, search_id: str) -> Search:
        search = await self.repo.get_search(search_id)
        if search is None or search.owner != owner:
            raise SupplyError(404, "search_not_found", "no such search")
        return await self._fresh(search)

    async def get_any(self, search_id: str) -> Search | None:
        return await self.repo.get_search(search_id)

    async def mine(self, owner: str, limit: int = 50) -> list[Search]:
        return [await self._fresh(s) for s in await self.repo.searches_of(owner, limit)]

    async def _fresh(self, search: Search) -> Search:
        if (
            search.status == "ready"
            and search.expires_at is not None
            and search.expires_at <= self.now()
        ):
            await self.repo.end_search(search.id)
            return Search(**{**search.__dict__, "status": "ended", "note": "expired"})
        return search

    async def expire(self) -> int:
        return await self.repo.expire_searches(self.now())

    async def pick(
        self,
        token: str | None,
        owner: str,
        search_id: str,
        refs: list[str],
        *,
        readings: tuple[str, ...],
        idempotency_key: str,
        follow_links: bool = False,
        deadline: datetime | None = None,
        subscriptions: Any = None,
    ) -> tuple[Demand, list[DemandItem], bool]:
        """勾候选：每条变成一个给网址的需求项。自己找的要本人确认（凭本人令牌请 knowbase
        记确认，kind=topic）；按主题订阅那一轮的凭订阅的范围同意（先问它还在不在）。
        先在幂等表与 `supply_search.status` 上原子占位，防止并发重复记确认（三审第 8 条）。"""
        search = await self.get(owner, search_id)
        by_ref = {str(c.get("ref")): c for c in search.candidates}
        chosen = [by_ref[r] for r in dict.fromkeys(refs) if r in by_ref]
        if not chosen or len(chosen) != len(set(refs)):
            raise refused("pick_invalid", "pick candidates of this search")
        digest = _pick_digest(
            search.id,
            [str(c.get("ref")) for c in chosen],
            readings,
            follow_links,
            deadline,
        )
        prior = await self.supply.claim_submission(
            owner, idempotency_key, digest, self.now()
        )
        resumed_confirmation: dict[str, object] | None = None
        if prior is not None:
            if prior["digest"] != digest:
                raise SupplyError(
                    409,
                    "idempotency_key_reused",
                    "this key was used for another request",
                )
            resumed = prior.get("resumed")
            if isinstance(resumed, dict):
                if isinstance(resumed.get("confirmed"), dict):
                    resumed_confirmation = dict(resumed["confirmed"])
            else:
                result = prior.get("result")
                demand_id = prior.get("demand_id")
                if not isinstance(result, dict) or not demand_id:
                    raise SupplyError(
                        409,
                        "submission_in_progress",
                        "the same request is being submitted",
                    )
                demand = await self.supply.get_demand(str(demand_id))
                assert demand is not None
                return demand, await self.supply.items_of(demand.id), True
        if resumed_confirmation is None:
            if search.status != "ready":
                await self.supply.drop_submission(owner, idempotency_key)
                raise SupplyError(409, "search_not_ready", "no candidates to pick now")
            if not await self.repo.claim_search_pick(search.id, owner):
                await self.supply.drop_submission(owner, idempotency_key)
                raise SupplyError(409, "search_not_ready", "no candidates to pick now")
        confirmed_saved = resumed_confirmation is not None
        try:
            if search.subscription_id is not None:
                sub = await subscriptions.repo.get(search.subscription_id)
                if sub is None or sub.status != "active":
                    raise SupplyError(
                        409, "subscription_not_active", "the subscription ended"
                    )
                today = self.now().astimezone(BEIJING).date()
                if today > sub.ends_on:
                    raise SupplyError(
                        409, "subscription_expired", "the subscription expired"
                    )
                live = await self.knowbase.check_subscription(
                    sub.id, sub.revision, owner
                )
                if live is None:
                    raise SupplyError(
                        409, "consent_withdrawn", "the consent was withdrawn"
                    )
                if live.get("status") == "expired":
                    raise SupplyError(
                        409, "subscription_expired", "the subscription expired"
                    )
                items = [
                    DemandItemIn(
                        None,
                        readings,
                        f"t{n}",
                        url=str(c["url"]),
                        follow_links=follow_links,
                        prefill={"title": str(c.get("title") or "")[:200]},
                        declared_public=True,
                    )
                    for n, c in enumerate(chosen, start=1)
                ]
                made = await self.demands.submit(
                    app="knowfeed",
                    owner=owner,
                    proposer="subscription",
                    chain=(f"subscription:{sub.id}", f"search:{search.id}"),
                    task_ref=None,
                    confirmation_id=sub.consent,
                    idempotency_key=idempotency_key,
                    items=items,
                    deadline=deadline,
                    consent_checked=True,
                )
            else:
                if resumed_confirmation is not None:
                    confirmation = resumed_confirmation
                else:
                    if self.user is None or token is None:
                        raise SupplyError(
                            503, "knowbase_unconfigured", "knowbase is not set up"
                        )
                    confirmation = await self.user.confirm(
                        token,
                        kind="topic",
                        items=[
                            {
                                "url": str(c["url"]),
                                "candidate_ref": f"{search.id}:{c['ref']}",
                                "follow_links": follow_links,
                                "title": str(c.get("title") or "")[:200],
                            }
                            for c in chosen
                        ],
                        supply={},
                    )
                    await self.supply.save_submission_progress(
                        owner, idempotency_key, {"confirmed": confirmation}
                    )
                    confirmed_saved = True
                ids = [str(i.get("item_id")) for i in confirmation.get("items") or []]
                if len(ids) != len(chosen):
                    raise SupplyError(
                        502, "unexpected_response", "knowbase confirmation"
                    )
                items = [
                    DemandItemIn(
                        None,
                        readings,
                        item_id,
                        url=str(c["url"]),
                        follow_links=follow_links,
                        prefill={"title": str(c.get("title") or "")[:200]},
                        declared_public=True,
                    )
                    for item_id, c in zip(ids, chosen, strict=True)
                ]
                made = await self.demands.submit(
                    app="knowfeed",
                    owner=owner,
                    proposer="user",
                    chain=(f"search:{search.id}",),
                    task_ref=None,
                    confirmation_id=str(confirmation["confirmation_id"]),
                    idempotency_key=idempotency_key,
                    items=items,
                    deadline=deadline,
                )
        except Exception:
            if not confirmed_saved:
                await self.repo.restore_search_ready(search.id)
                await self.supply.drop_submission(owner, idempotency_key)
            raise
        demand, made_items, _ = made
        await self.supply.finish_submission(
            owner, idempotency_key, demand.id, {"picked": True}
        )
        return demand, made_items, False


def _pick_digest(
    search_id: str,
    refs: list[str],
    readings: tuple[str, ...],
    follow_links: bool,
    deadline: datetime | None,
) -> str:
    body = {
        "search_id": search_id,
        "refs": refs,
        "readings": sorted(readings),
        "follow_links": follow_links,
        "deadline": deadline.isoformat() if deadline else None,
    }
    text = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _searchable(channel: Channel) -> bool:
    return (
        channel.kind == "search"
        and channel.review == "approved"
        and channel.status == "active"
    )


class SearchRunner:
    """作业里跑一次站内搜索（`jobs.RoundWork`）：记候选，每条先预切合（网站审没审过、
    许可哪一档）；这次搜索的作业都跑完了就转「候选已出」，7 天内勾。"""

    def __init__(
        self,
        repo: Any,
        catalog: CatalogRepository,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repo = repo
        self.catalog = catalog
        self.now = clock

    async def run(self, job: Job, channel: Channel, adapter: Any, ask: Ask) -> str:
        found = await ask(
            channel,
            adapter.search(str(job.payload["topic"]), int(job.payload["wanted"])),  # type: ignore[arg-type]
        )
        marked = []
        for n, candidate in enumerate(found):
            marked.append(
                {
                    **candidate,
                    "order": n,
                    "channel_priority": channel.priority,
                    **await self._site(str(candidate["url"])),
                }
            )
        await self._record(job, marked, None)
        return "done"

    async def give_up(self, job: Job, reason: str) -> None:
        await self._record(job, [], f"{job.channel_id}:{reason}")

    async def _record(
        self, job: Job, found: list[dict[str, object]], note: str | None
    ) -> None:
        now = self.now()
        await self.repo.add_candidates(
            str(job.payload["search_id"]),
            job.id,
            found,
            note=note,
            ready_at=now,
            expires_at=now + CANDIDATES_VALID,
        )

    async def _site(self, url: str) -> dict[str, object]:
        """候选的预切合（只查来源目录）：落在哪条渠道、网站审没审过、许可哪一档。"""
        try:
            channel = await self.catalog.channel_for_host(site_of(url))
        except SupplyError:
            return {"site": "invalid"}
        if channel is None:
            return {"site": "unregistered"}
        if channel.review == "approved" and channel.status != "disabled":
            return {
                "site": "approved",
                "site_channel": channel.id,
                "license": channel.license,
            }
        if channel.review == "pending":
            return {"site": "pending", "site_channel": channel.id}
        return {"site": "rejected", "site_channel": channel.id}
