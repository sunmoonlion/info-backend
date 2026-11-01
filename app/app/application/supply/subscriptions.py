"""订阅（SDD 0045 第 2.2、2.5 节，第 3.3 节，第七节块 9；发版前补齐 F1）。

**建、续、撤凭本人令牌**：本人在 knowfeed web端建订阅时，knowfeed 凭本人令牌在 knowbase
留一条长期范围同意（`subscription:<订阅>@<修订>`，0043 第 2.2 节）；续订 = 同意出新修订；
撤订阅同时撤同意。范围内每轮不再逐份确认。

**每一轮**（beat 每分钟 `tick` 一次）：
1. 到点的订阅：过了截止日的转「已到期」，它生成的在途需求项标「已取消（订阅到期）」；
   先问 knowbase 那条范围同意还在不在（本人可能已在 knowbase 撤了），不在就转「被停用」、
   在途项取消，不再生成新需求；暂停的只往后推。
2. 按描述订、按网址订：给每个订阅对象排一个「看新的」作业（第 ③ 档，跨订阅共享，同一
   渠道同一对象的游标大家共用，`watch.py`、`web.py`）；按主题订：排一次站内搜索
   （`searches.py`），候选出来要本人勾，7 天不勾作废。
3. 作业都跑完了：取这条订阅还没处理过的发现——只收起始日之后的；本月上限以内的生成
   需求项（已知渠道、已知条目，直接建取数作业），超出的记「本月超额未取」，下月初优先补取
   （本人选了跳过的就跳过）；记这一轮查的时间、发现几份、取了几份、因上限没取几份。

查的频率取用户希望的与渠道允许的最快里慢的那个（`subscription.every`）。提前 14 天提醒续订：
web端「订阅」页按 `renew_due` 显示（knowfeed 没有站内信通道，见交回报告）。
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime, time
from typing import Any

from app.application.ports.supply import (
    CatalogRepository,
    Knowbase,
    KnowbaseUser,
    SupplyRepository,
)
from app.application.supply.jobs import Ask
from app.application.supply.watch import found_id, watch_catalog
from app.domain.supply.catalog import site_of
from app.domain.supply.models import (
    REASON_SUBSCRIPTION_ENDED,
    REASON_SUBSCRIPTION_EXPIRED,
    Channel,
    Demand,
    DemandItem,
    DescriptionKey,
    Entry,
    Job,
    SupplyError,
    refused,
)
from app.domain.supply.quota import BEIJING, Quota, month_start
from app.domain.supply.schedule import TIER_SUBSCRIPTION, Conditions, candidates
from app.domain.supply.subscription import (
    LIVE,
    MAX_BACKLOG,
    MAX_SPAN,
    Round,
    Subscription,
    check_subscription,
    every,
)
from app.domain.supply.vocabulary import is_periodic

log = logging.getLogger(__name__)

BATCH = 50
SEARCH_WANTED = 20  # 按主题订阅每轮要几个候选


def watch_key(channel_id: str, target: str) -> str:
    digest = hashlib.sha256(target.encode("utf-8")).hexdigest()[:40]
    return f"watch:{channel_id}:{digest}"


def today_of(now: datetime) -> date:
    return now.astimezone(BEIJING).date()


class SubscriptionService:
    def __init__(
        self,
        repo: Any,
        supply: SupplyRepository,
        catalog: CatalogRepository,
        knowbase: Knowbase,
        user: KnowbaseUser | None,
        searches: Any,
        *,
        quota: Quota | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self.repo = repo
        self.supply = supply
        self.catalog = catalog
        self.knowbase = knowbase
        self.user = user
        self.searches = searches
        self.quota = quota or Quota()
        self.now = clock
        self.new_id = new_id

    # —— 本人：建、看、暂停、恢复、续、撤 ——

    async def create(
        self,
        token: str,
        owner: str,
        *,
        by: str,
        subjects: tuple[str, ...] = (),
        doc_types: tuple[str, ...] = (),
        urls: tuple[str, ...] = (),
        topic: str | None = None,
        readings: tuple[str, ...] = ("original",),
        starts_on: date | None = None,
        ends_on: date,
        every_hours: int = 24,
        monthly_cap: int = 100,
        purpose: str,
        skip_backlog: bool = False,
        idempotency_key: str | None = None,
    ) -> Subscription:
        now = self.now()
        today = today_of(now)
        starts = starts_on or today
        check_subscription(
            by=by,
            subjects=subjects,
            doc_types=doc_types,
            urls=urls,
            topic=topic,
            readings=readings,
            starts_on=starts,
            ends_on=ends_on,
            every_hours=every_hours,
            monthly_cap=monthly_cap,
            purpose=purpose,
            today=today,
        )
        if monthly_cap > self.quota.monthly_items:
            raise refused(
                "monthly_cap_invalid",
                f"at most {self.quota.monthly_items} a month (your quota)",
            )
        urls = tuple(dict.fromkeys(urls))
        clean_subjects = tuple(dict.fromkeys(subjects))
        clean_doc_types = tuple(dict.fromkeys(doc_types))
        clean_readings = tuple(dict.fromkeys(readings))
        clean_purpose = purpose.strip()
        clean_topic = topic.strip() if topic else None
        digest = _sub_digest(
            by=by,
            subjects=clean_subjects,
            doc_types=clean_doc_types,
            urls=urls,
            topic=clean_topic,
            readings=clean_readings,
            starts_on=starts,
            ends_on=ends_on,
            every_hours=every_hours,
            monthly_cap=monthly_cap,
            purpose=clean_purpose,
            skip_backlog=skip_backlog,
        )
        resumed_sub_id: str | None = None
        if idempotency_key is not None:
            prior = await self.supply.claim_submission(
                owner, idempotency_key, digest, now
            )
            if prior is not None:
                if prior["digest"] != digest:
                    raise SupplyError(
                        409,
                        "idempotency_key_reused",
                        "this key was used for another request",
                    )
                resumed = prior.get("resumed")
                if isinstance(resumed, dict) and isinstance(
                    resumed.get("subscription_id"), str
                ):
                    resumed_sub_id = str(resumed["subscription_id"])
                else:
                    result = prior.get("result")
                    sub_id = (
                        result.get("subscription_id")
                        if isinstance(result, dict)
                        else None
                    )
                    if not isinstance(sub_id, str):
                        raise SupplyError(
                            409,
                            "submission_in_progress",
                            "the same subscription is being created",
                        )
                    found = await self.repo.get(sub_id)
                    if found is not None:
                        return found
        if self.user is None:
            if idempotency_key is not None:
                await self.supply.drop_submission(owner, idempotency_key)
            raise SupplyError(503, "knowbase_unconfigured", "knowbase is not set up")
        check_at = max(now, _midnight(starts))
        draft = Subscription(
            id=resumed_sub_id or self.new_id(),
            owner=owner,
            by=by,
            readings=clean_readings,
            every_hours=every_hours,
            starts_on=starts,
            ends_on=ends_on,
            monthly_cap=monthly_cap,
            status="active",
            revision=1,
            purpose=clean_purpose,
            created_at=now,
            updated_at=now,
            subjects=clean_subjects,
            doc_types=clean_doc_types,
            urls=urls,
            topic=clean_topic,
            skip_backlog=skip_backlog,
            next_check_at=check_at,
        )
        try:
            sub = replace(draft, channels=await self._executors(draft))
            if resumed_sub_id is None or await self.repo.get(sub.id) is None:
                # 先在同用户配额锁里占住本地订阅名额（next_check_at=None 暂不被 tick 扫到），
                # 再调 knowbase 记范围同意；失败则删回占位（三审第 8 条）
                await self.repo.add(
                    replace(sub, next_check_at=None),
                    max_live=self.quota.subscriptions,
                )
            if idempotency_key is not None:
                await self.supply.save_submission_progress(
                    owner, idempotency_key, {"subscription_id": sub.id}
                )
        except Exception:
            if idempotency_key is not None:
                await self.supply.drop_submission(owner, idempotency_key)
            raise
        try:
            await self.user.subscribe(token, sub.id, sub.revision, sub.scope())
        except Exception:
            await self.repo.remove(sub.id)
            if idempotency_key is not None:
                await self.supply.drop_submission(owner, idempotency_key)
            raise
        ready_sub = (
            await self.repo.change(
                sub.id, {"next_check_at": check_at, "updated_at": now}
            )
            or sub
        )
        if idempotency_key is not None:
            await self.supply.finish_submission(
                owner, idempotency_key, None, {"subscription_id": ready_sub.id}
            )
        return ready_sub

    async def _executors(self, sub: Subscription) -> dict[str, str]:
        """建订阅时找能执行的渠道（0045 第四节）：按描述订找会「看新的」的目录型渠道，按网址
        订找那个网站登记的订阅源渠道，按主题订找审过的站内搜索。找不到订阅建不起来。"""
        channels = await self.supply.channels()
        found: dict[str, str] = {}
        if sub.by == "description":
            for target in sub.targets():
                subject, doc_type = target.split("|", 1)
                _, allowed = candidates(
                    channels, DescriptionKey(subject, doc_type), Conditions()
                )
                watchers = [c for c in allowed if c.kind == "catalog"]
                if not watchers:
                    raise refused("no_watch_channel", f"nothing can watch {target}")
                found[target] = watchers[0].id
        elif sub.by == "url":
            for url in sub.urls:
                channel = await self.catalog.channel_for_host(site_of(url))
                if (
                    channel is None
                    or channel.kind != "feed"
                    or channel.review != "approved"
                    or channel.status == "disabled"
                ):
                    raise refused("no_watch_channel", f"no feed channel for {url}")
                found[url] = channel.id
        else:
            if not [c for c in channels if _searchable(c)]:
                raise refused("no_watch_channel", "no site search is approved")
        return found

    async def mine(self, owner: str) -> list[Subscription]:
        return await self.repo.of_owner(owner)

    async def get(
        self, owner: str, subscription_id: str
    ) -> tuple[Subscription, list[Round]]:
        sub = await self._own(owner, subscription_id)
        return sub, await self.repo.rounds(sub.id, 30)

    async def _own(self, owner: str, subscription_id: str) -> Subscription:
        sub = await self.repo.get(subscription_id)
        if sub is None or sub.owner != owner:
            raise SupplyError(404, "subscription_not_found", "no such subscription")
        return sub

    async def pause(self, owner: str, subscription_id: str) -> Subscription:
        sub = await self._own(owner, subscription_id)
        changed = await self.repo.change(
            sub.id,
            {"status": "paused", "updated_at": self.now()},
            when_status=("active",),
        )
        if changed is None:
            raise SupplyError(409, "subscription_not_active", "only active ones pause")
        return changed

    async def resume(self, owner: str, subscription_id: str) -> Subscription:
        """暂停后从自己的进度恢复：暂停期间发现的，起始日之后的照样补上。"""
        sub = await self._own(owner, subscription_id)
        now = self.now()
        changed = await self.repo.change(
            sub.id,
            {"status": "active", "updated_at": now, "next_check_at": now},
            when_status=("paused",),
        )
        if changed is None:
            raise SupplyError(409, "subscription_not_paused", "only paused ones resume")
        return changed

    async def set_skip_backlog(
        self, owner: str, subscription_id: str, skip: bool
    ) -> Subscription:
        sub = await self._own(owner, subscription_id)
        changed = await self.repo.change(
            sub.id, {"skip_backlog": skip, "updated_at": self.now()}
        )
        assert changed is not None
        return changed

    async def renew(
        self, token: str, owner: str, subscription_id: str, ends_on: date
    ) -> Subscription:
        """续订 = 范围同意出新修订（0045 第 2.2 节）；新的这一期最长一年，从今天算。旧修订
        留着：上一期生成的、还在路上的需求凭的是它，撤订阅时各修订一起撤。"""
        sub = await self._own(owner, subscription_id)
        if sub.status not in (*LIVE, "expired"):
            raise SupplyError(409, "subscription_ended", "cancelled or disabled")
        today = today_of(self.now())
        if ends_on <= max(today, sub.ends_on) or ends_on - today > MAX_SPAN:
            raise refused("ends_on_invalid", "give a later end date within one year")
        if self.user is None:
            raise SupplyError(503, "knowbase_unconfigured", "knowbase is not set up")
        now = self.now()
        revision = sub.revision + 1
        scope = {
            **replace(sub, ends_on=ends_on).scope(),
            "starts_on": max(today, sub.starts_on).isoformat(),
        }
        await self.user.subscribe(token, sub.id, revision, scope)
        values: dict[str, object] = {
            "revision": revision,
            "ends_on": ends_on,
            "status_reason": None,
            "updated_at": now,
        }
        if sub.status == "expired":
            values.update(status="active", next_check_at=now)
        changed = await self.repo.change(
            sub.id,
            values,
            when_status=(*LIVE, "expired"),
            when_revision=sub.revision,
        )
        if changed is None:
            raise SupplyError(409, "subscription_changed", "it changed; try again")
        return changed

    async def cancel(
        self, token: str, owner: str, subscription_id: str
    ) -> Subscription:
        """撤订阅：撤 knowbase 里的范围同意——各修订都撤（续订过的旧修订也撤，二审第 15
        条），不再生成新需求，它生成的在途项取消。"""
        sub = await self._own(owner, subscription_id)
        if sub.status in ("cancelled",):
            return sub
        if self.user is None:
            raise SupplyError(503, "knowbase_unconfigured", "knowbase is not set up")
        for revision in range(sub.revision, 0, -1):
            await self.user.unsubscribe(token, sub.id, revision)
        now = self.now()
        changed = await self.repo.change(
            sub.id,
            {
                "status": "cancelled",
                "round_id": None,
                "updated_at": now,
                "status_reason": None,
            },
        )
        assert changed is not None
        await self.repo.end_items(changed, REASON_SUBSCRIPTION_ENDED, now)
        return changed

    # —— 每一轮 ——

    async def tick(self) -> dict[str, int]:
        done = {"started": 0, "finished": 0, "ended": 0}
        for sub in await self.repo.watching(BATCH):
            try:
                res = await self._finish(sub)
                if res is True:
                    done["finished"] += 1
                elif res == "ended":
                    done["ended"] += 1
            except Exception:
                log.exception("supply_subscription_finish_failed id=%s", sub.id)
        for sub in await self.repo.due(self.now(), BATCH):
            try:
                outcome = await self._start(sub)
                done[outcome] = done.get(outcome, 0) + 1
            except Exception:
                log.exception("supply_subscription_round_failed id=%s", sub.id)
        if self.searches is not None:
            await self.searches.expire()
        return done

    async def _end(self, sub: Subscription, status: str, reason: str | None) -> str:
        """订阅结束：到期的在途项标「已取消（订阅到期）」，撤了同意的标「已取消（订阅撤了）」。"""
        now = self.now()
        changed = await self.repo.change(
            sub.id,
            {
                "status": status,
                "status_reason": reason,
                "round_id": None,
                "updated_at": now,
            },
            when_status=tuple(LIVE),
        )
        if changed is not None:
            await self.repo.end_items(
                changed,
                REASON_SUBSCRIPTION_EXPIRED
                if status == "expired"
                else REASON_SUBSCRIPTION_ENDED,
                now,
            )
        return "ended"

    async def _consent_state(self, sub: Subscription, today: date) -> str:
        """问 knowbase 范围同意还在不在：区分『到期』（expired）与『撤回』（withdrawn）（三审第 10 条）。"""
        if today > sub.ends_on:
            return "expired"
        try:
            live = await self.knowbase.check_subscription(
                sub.id, sub.revision, sub.owner
            )
        except SupplyError as exc:
            if exc.code in ("subscription_expired", "consent_expired"):
                return "expired"
            raise
        if live is None:
            return "withdrawn"
        if live.get("status") == "expired":
            return "expired"
        ends_raw = live.get("ends_on")
        if isinstance(ends_raw, str):
            try:
                if date.fromisoformat(ends_raw) < today:
                    return "expired"
            except ValueError:
                pass
        return "ok"

    async def _start(self, sub: Subscription) -> str:
        now = self.now()
        today = today_of(now)
        if today > sub.ends_on:
            return await self._end(sub, "expired", None)
        step = every(sub.every_hours, {})
        if sub.round_id is not None:
            return "running"  # 没过期、这一轮还在跑（due 把过期的也选进来了）
        if sub.status == "paused":
            await self.repo.change(
                sub.id, {"next_check_at": now + step}, when_status=("paused",)
            )
            return "paused"
        if today < sub.starts_on:
            await self.repo.change(sub.id, {"next_check_at": _midnight(sub.starts_on)})
            return "waiting"
        consent = await self._consent_state(sub, today)
        if consent == "expired":
            return await self._end(sub, "expired", None)
        if consent == "withdrawn":
            # 本人在 knowbase 撤了范围同意：不再生成新需求
            return await self._end(sub, "disabled", "consent_withdrawn")
        channels = {c.id: c for c in await self.supply.channels()}
        round_ = Round(id=self.new_id(), subscription_id=sub.id, started_at=now)
        if sub.by == "topic":
            if not await self.repo.start_round(sub, round_, []):
                return "running"
            search = await self.searches.start(
                sub.owner, str(sub.topic), (), SEARCH_WANTED, subscription=sub
            )
            await self.repo.set_round_search(sub.id, round_.id, search.id, now)
            return "started"
        jobs = []
        for target, channel_id in sub.channels.items():
            channel = channels.get(channel_id)
            if channel is None:
                continue
            jobs.append(
                Job(
                    id=self.new_id(),
                    kind="watch",
                    channel_id=channel_id,
                    dedup_key=watch_key(channel_id, target),
                    payload={"target": target, "owner": sub.owner},
                    status="queued",
                    attempts=0,
                    created_at=now,
                    tier=TIER_SUBSCRIPTION,
                )
            )
        if not await self.repo.start_round(sub, round_, jobs):
            return "running"
        return "started"

    async def _finish(self, sub: Subscription) -> bool | str:
        """这一轮的作业都跑完了：收这条订阅的新发现，生成需求单，结这一轮。"""
        now = self.now()
        today = today_of(now)
        if today > sub.ends_on:
            await self._end(sub, "expired", None)
            return "ended"
        rounds = await self.repo.rounds(sub.id, 1)
        if not rounds or rounds[0].id != sub.round_id:
            return False
        round_ = rounds[0]
        channels = {c.id: c for c in await self.supply.channels()}
        step = max(
            (
                every(sub.every_hours, c.config)
                for c in channels.values()
                if c.id in sub.channels.values()
            ),
            default=every(sub.every_hours, {}),
        )
        if sub.by == "topic":
            search = await self.searches.get_any(sub.channels.get("search", ""))
            if search is not None and search.status == "searching":
                return False
            note = f"search:{search.id}" if search is not None else "search_missing"
            return await self.repo.finish_round(
                sub,
                replace(
                    round_, found=len(search.candidates) if search else 0, note=note
                ),
                [],
                None,
                next_check_at=now + step,
                at=now,
            )
        keys = [watch_key(cid, target) for target, cid in sub.channels.items()]
        if await self.repo.open_jobs(keys):
            return False
        pairs = [(cid, target) for target, cid in sub.channels.items()]
        used = await self.repo.taken_since(sub, month_start(now))
        room = max(0, sub.monthly_cap - used)
        takes: list[tuple[str, str, dict[str, object], str]] = []
        chosen: list[tuple[str, dict[str, object], str]] = []
        for target, entry in await self.repo.backlog(sub):
            doc_id = str(entry.get("found_id") or entry["channel_doc_id"])
            if sub.skip_backlog:
                takes.append((target, "skipped", entry, doc_id))
            elif room > 0:
                takes.append((target, "taken", entry, doc_id))
                chosen.append((target, entry, doc_id))
                room -= 1
        found = over = 0
        backlog_left = MAX_BACKLOG
        for target, entry, disclosed, found_at in await self.repo.fresh(sub, pairs):
            doc_id = str(entry.get("found_id") or entry["channel_doc_id"])
            day = disclosed or (
                None if entry.get("baseline") else found_at.astimezone(BEIJING).date()
            )
            if day is None or day < sub.starts_on:
                # 新加入的只收起始日之后的（栏目页第一次看到的旧链接不算新的）
                takes.append((target, "skipped", entry, doc_id))
                continue
            found += 1
            if room > 0:
                takes.append((target, "taken", entry, doc_id))
                chosen.append((target, entry, doc_id))
                room -= 1
            elif backlog_left > 0:
                takes.append((target, "over_cap", entry, doc_id))
                over += 1
                backlog_left -= 1
            else:
                takes.append((target, "skipped", entry, doc_id))
        if chosen:
            consent = await self._consent_state(sub, today)
            if consent == "expired":
                await self._end(sub, "expired", None)
                return "ended"
            if consent == "withdrawn":
                # 这一轮跑的中途本人撤了范围同意：不生成需求单（二审第 8 条）
                await self._end(sub, "disabled", "consent_withdrawn")
                return "ended"
        demand = self._demand(sub, round_, chosen, channels, now) if chosen else None
        item_of = (
            {
                doc_id: item.id
                for (_, _, doc_id), item in zip(chosen, demand[1], strict=True)
            }
            if demand
            else {}
        )
        return await self.repo.finish_round(
            sub,
            replace(round_, found=found, taken=len(chosen), over_cap=over),
            [
                (t, st, e, d, item_of.get(d) if st == "taken" else None)
                for t, st, e, d in takes
            ],
            demand,
            next_check_at=now + step,
            at=now,
            owner_monthly=self.quota.monthly_items,
        )

    def _demand(
        self,
        sub: Subscription,
        round_: Round,
        chosen: list[tuple[str, dict[str, object], str]],
        channels: dict[str, Channel],
        now: datetime,
    ) -> tuple[Demand, list[DemandItem], list[Job], dict[str, str]]:
        """这一轮要取的生成一张需求单（提出方「订阅」、凭范围同意，第 ③ 档）；条目已知，
        取数作业不必再「列」。"""
        demand = Demand(
            id=self.new_id(),
            owner=sub.owner,
            proposer="subscription",
            chain=(f"subscription:{sub.id}", f"round:{round_.id}"),
            app="knowfeed",
            task_ref=None,
            confirmation_id=sub.consent,
            idempotency_key=f"sub-round:{round_.id}",
            digest=hashlib.sha256(round_.id.encode()).hexdigest(),
            created_at=now,
        )
        items: list[DemandItem] = []
        jobs: dict[str, Job] = {}
        links: dict[str, str] = {}
        for n, (_, raw, doc_id) in enumerate(chosen, start=1):
            entry = Entry.from_dict(raw)
            key = _key(sub, raw, entry)
            payload: dict[str, object] = {"entry": entry.as_dict()}
            if key is not None:
                payload["key"] = key.as_dict()
            dedup = f"fetch:{entry.channel_id}:doc:{doc_id}"[:255]
            jobs.setdefault(
                dedup,
                Job(
                    id=self.new_id(),
                    kind="fetch",
                    channel_id=entry.channel_id,
                    dedup_key=dedup,
                    payload=payload,
                    status="queued",
                    attempts=0,
                    created_at=now,
                    tier=TIER_SUBSCRIPTION,
                ),
            )
            item_id = self.new_id()
            links[item_id] = dedup
            items.append(
                DemandItem(
                    id=item_id,
                    demand_id=demand.id,
                    key=key,
                    readings=sub.readings,
                    confirmation_item=f"s{n}",
                    status="fetching",
                    created_at=now,
                    updated_at=now,
                    channel_id=entry.channel_id,
                    url=None if key is not None else entry.url,
                )
            )
        return demand, items, list(jobs.values()), links


def _key(
    sub: Subscription, raw: dict[str, object], entry: Entry
) -> DescriptionKey | None:
    """按描述订的条目落成精确描述：定期报告用列出它的那条描述（报告期），别的按披露那天。"""
    if sub.by != "description":
        return None
    listed = raw.get("key")
    if isinstance(listed, dict) and is_periodic(str(listed.get("doc_type"))):
        return DescriptionKey.from_dict(listed)
    subject = str(listed.get("subject")) if isinstance(listed, dict) else ""
    return DescriptionKey(
        subject or str(entry.subject or ""),
        entry.doc_type,
        entry.disclosed_on,
        entry.disclosed_on,
        edition=entry.edition,
        topics=entry.topics,
    )


def _midnight(day: date) -> datetime:
    return datetime.combine(day, time(0, 0), tzinfo=BEIJING)


def _sub_digest(
    *,
    by: str,
    subjects: tuple[str, ...],
    doc_types: tuple[str, ...],
    urls: tuple[str, ...],
    topic: str | None,
    readings: tuple[str, ...],
    starts_on: date,
    ends_on: date,
    every_hours: int,
    monthly_cap: int,
    purpose: str,
    skip_backlog: bool,
) -> str:
    body = {
        "by": by,
        "subjects": list(subjects),
        "doc_types": list(doc_types),
        "urls": list(urls),
        "topic": topic,
        "readings": sorted(readings),
        "starts_on": starts_on.isoformat(),
        "ends_on": ends_on.isoformat(),
        "every_hours": every_hours,
        "monthly_cap": monthly_cap,
        "purpose": purpose,
        "skip_backlog": skip_backlog,
    }
    text = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _searchable(channel: Channel) -> bool:
    return (
        channel.kind == "search"
        and channel.review == "approved"
        and channel.status == "active"
    )


class WatchRunner:
    """作业里跑一次「看新的」（`jobs.RoundWork`）：记新发现与新游标，订同一个对象的人共用。"""

    def __init__(
        self,
        repo: Any,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repo = repo
        self.now = clock

    async def run(self, job: Job, channel: Channel, adapter: Any, ask: Ask) -> str:
        target = str(job.payload["target"])
        cursor = await self.repo.cursor(channel.id, target)
        found: list[tuple[str, dict[str, object], date | None]] = []
        if channel.kind == "catalog":
            pairs, fresh_cursor = await ask(
                channel, watch_catalog(adapter, target, cursor, today_of(self.now()))
            )
            for entry, key in pairs:
                doc_id = found_id(entry)
                found.append(
                    (
                        doc_id,
                        {**entry.as_dict(), "key": key.as_dict(), "found_id": doc_id},
                        entry.disclosed_on,
                    )
                )
        else:
            entries, fresh_cursor = await ask(channel, adapter.watch(target, cursor))
            baseline = set(fresh_cursor.get("baseline") or [])
            for entry in entries:
                found.append(
                    (
                        entry.channel_doc_id,
                        {
                            **entry.as_dict(),
                            "found_id": entry.channel_doc_id,
                            "baseline": entry.channel_doc_id in baseline,
                        },
                        entry.disclosed_on,
                    )
                )
        await self.repo.save_watch(channel.id, target, found, fresh_cursor, self.now())
        return "done"

    async def give_up(self, job: Job, reason: str) -> None:
        # 这一轮这个对象没看成：订阅照样结这一轮（只收已有的发现），下一轮再看
        log.warning("supply_watch_failed job=%s reason=%s", job.id, reason)
