"""来源目录的管理（SDD 0045 第 3.1 节、第七节块 2）：管理端登记渠道、审核与试采、在用 / 暂停
/ 停用、排先后；按网站审与待审网站；作业里连续失败自动暂停。

- 审核就是第一道关：每个渠道审一次。放行之前要有一次试采成功（「列」出条目、「取」回
  内容），审核的人要写凭什么。
- 停用（或审核撤销）：渠道上没办完的作业取消，在等它的需求项转「无法满足」附原因；
  暂停只让作业等着，恢复后接着跑。
- 试采要出公网，放在 worker 里跑（API 不出公网）：管理端点了排一条，页面看结果。
- 通用网页按网站审：用户给的网址落在没登记的网站上，`ensure_site` 建一条「待审」渠道
  （给网址这种需求随块 4、块 10 接上时调它）；待审超过时限，`expire_sites` 让在等的
  需求项转「无法满足」。

每一次改动都记一条审核记录：谁、何时、做了什么、凭什么。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from app.application.ports.supply import Catalog, CatalogRepository
from app.domain.supply.catalog import (
    SITE_REVIEW_WITHIN,
    ChannelEvent,
    Registration,
    SiteSummary,
    Trial,
    check_channel_id,
    check_registration,
    move_status,
    site_channel_id,
    site_of,
)
from app.domain.supply.models import (
    REASON_CHANNEL_DISABLED,
    REASON_SITE_REJECTED,
    Channel,
    DescriptionKey,
    SupplyError,
    refused,
)
from app.domain.supply.vocabulary import DOC_TYPE_CLASS

log = logging.getLogger(__name__)

# 有哪些适配器（插件名 → 大类）。加渠道是登记一条记录，选现有的插件加配置；
# 通用网页型适配器随块 3 接上，在那之前「web」渠道只能登记与审核，试采不了
PLUGINS: dict[str, str] = {"cninfo": "catalog", "web": "web"}
TRIAL_LISTED = 5  # 试采结果里列出几条


def _known_types() -> frozenset[str]:
    return frozenset(DOC_TYPE_CLASS)


class ChannelCatalog:
    def __init__(
        self,
        repo: CatalogRepository,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self.repo = repo
        self.now = clock
        self.new_id = new_id

    async def channels(self) -> list[Channel]:
        return await self.repo.channels()

    async def get(self, channel_id: str) -> Channel:
        channel = await self.repo.get_channel(channel_id)
        if channel is None:
            raise SupplyError(404, "channel_not_found", "no such channel")
        return channel

    async def detail(
        self, channel_id: str
    ) -> tuple[Channel, list[ChannelEvent], list[Trial]]:
        channel = await self.get(channel_id)
        return (
            channel,
            await self.repo.events(channel_id, limit=50),
            await self.repo.trials(channel_id, limit=10),
        )

    def _event(
        self,
        channel_id: str,
        action: str,
        actor: str,
        reason: str | None,
        detail: dict[str, object] | None = None,
    ) -> ChannelEvent:
        return ChannelEvent(
            id=self.new_id(),
            channel_id=channel_id,
            action=action,
            actor=actor,
            reason=(reason or None) and reason[:500],
            detail=detail or {},
            at=self.now(),
        )

    async def register(
        self, channel_id: str, registration: Registration, *, actor: str
    ) -> Channel:
        """登记一条渠道：待审、在用；排在现有的后面。审过才会被切合用上。"""
        check_channel_id(channel_id)
        reg = check_registration(registration, _known_types())
        _check_plugin(reg)
        now = self.now()
        existing = await self.repo.channels()
        channel = Channel(
            id=channel_id,
            name=reg.name,
            kind=reg.kind,  # type: ignore[arg-type]
            plugin=reg.plugin,
            doc_types=reg.doc_types,
            readings=reg.readings,
            license=reg.license,
            review="pending",
            rate_per_minute=reg.rate_per_minute,
            daily_cap=reg.daily_cap,
            priority=max((c.priority for c in existing), default=0) + 10,
            domain=reg.domain,
            terms_url=reg.terms_url,
            render_allowed=reg.render_allowed,
            obey_robots=reg.obey_robots,
            config=reg.config,
            created_at=now,
            updated_at=now,
        )
        await self.repo.add_channel(
            channel, self._event(channel_id, "register", actor, None, _shape(channel))
        )
        return channel

    async def update(
        self, channel_id: str, registration: Registration, *, actor: str, reason: str
    ) -> Channel:
        """改登记（能提供什么、怎么取、条款与许可、规矩）。大类、插件不能改：换了就是另一条
        渠道。改过的要重新试采才能再放行。"""
        current = await self.get(channel_id)
        reg = check_registration(registration, _known_types())
        if (reg.kind, reg.plugin) != (current.kind, current.plugin):
            raise refused("kind_fixed", "register another channel to change its kind")
        _need_reason(reason)
        changed = replace(
            current,
            name=reg.name,
            doc_types=reg.doc_types,
            readings=reg.readings,
            license=reg.license,
            rate_per_minute=reg.rate_per_minute,
            daily_cap=reg.daily_cap,
            domain=reg.domain,
            terms_url=reg.terms_url,
            render_allowed=reg.render_allowed,
            obey_robots=reg.obey_robots,
            config=reg.config,
            updated_at=self.now(),
        )
        before, after = _shape(current), _shape(changed)
        diff = {
            k: {"from": before[k], "to": after[k]}
            for k in after
            if before[k] != after[k]
        }
        await self.repo.save_channel(
            changed, self._event(channel_id, "update", actor, reason, diff)
        )
        return changed

    async def review(
        self,
        channel_id: str,
        decision: str,
        *,
        actor: str,
        reason: str,
        license: str | None = None,
    ) -> Channel:
        """审核：放行（白名单）或审不过。放行要最近一次试采成功；都要写凭什么。

        审不过（包括撤销已放行的）：渠道上没办完的作业取消，在等的需求项转「无法满足」。"""
        current = await self.get(channel_id)
        _need_reason(reason)
        if decision == "approve":
            trial = await self.repo.latest_trial(channel_id)
            if trial is None or trial.status != "ok":
                raise SupplyError(
                    409, "trial_required", "run a successful trial before approving"
                )
            changed_at = await self.repo.registration_changed_at(channel_id)
            if changed_at is not None and trial.created_at < changed_at:
                raise SupplyError(
                    409, "trial_outdated", "the registration changed after the trial"
                )
            chosen = license or current.license
            if chosen not in ("self", "via_expert", "sale"):
                raise refused("license_invalid", "license is self/via_expert/sale")
            changed = replace(
                current, review="approved", license=chosen, updated_at=self.now()
            )
            await self.repo.save_channel(
                changed,
                self._event(
                    channel_id,
                    "approve",
                    actor,
                    reason,
                    {"license": chosen, "trial_id": trial.id},
                ),
            )
            return changed
        if decision != "reject":
            raise refused("decision_invalid", "decision is approve or reject")
        changed = replace(current, review="rejected", updated_at=self.now())
        withdraw = (
            REASON_SITE_REJECTED if current.kind == "web" else REASON_CHANNEL_DISABLED
        )
        await self.repo.save_channel(
            changed,
            self._event(channel_id, "reject", actor, reason),
            withdraw=withdraw,
        )
        return changed

    async def set_status(
        self, channel_id: str, action: str, *, actor: str, reason: str | None
    ) -> Channel:
        """在用 / 暂停 / 停用。暂停、停用要写原因；恢复时连续失败次数清零、等着的作业接着排。"""
        current = await self.get(channel_id)
        target = move_status(current.status, action, current.review)
        if action in ("pause", "disable"):
            _need_reason(reason or "")
        changed = replace(
            current,
            status=target,  # type: ignore[arg-type]
            status_reason=None if target == "active" else (reason or "")[:200],
            consecutive_failures=0
            if target == "active"
            else current.consecutive_failures,
            updated_at=self.now(),
        )
        await self.repo.save_channel(
            changed,
            self._event(channel_id, action, actor, reason),
            withdraw=REASON_CHANNEL_DISABLED if target == "disabled" else None,
            requeue=target == "active",
        )
        return changed

    async def reorder(self, channel_ids: list[str], *, actor: str) -> list[Channel]:
        """排先后：给出的顺序就是切合时试的顺序（官方在前、许可宽的在前，由审核的人定）。
        没列到的排在后面，各自的相对顺序不变。"""
        current = await self.repo.channels()
        known = {c.id for c in current}
        unknown = [i for i in channel_ids if i not in known]
        if unknown or len(set(channel_ids)) != len(channel_ids):
            raise refused("order_invalid", "list each existing channel once")
        rest = [c.id for c in current if c.id not in channel_ids]
        order = [*channel_ids, *rest]
        priorities = {cid: (n + 1) * 10 for n, cid in enumerate(order)}
        events = [
            self._event(
                c.id,
                "reorder",
                actor,
                None,
                {"from": c.priority, "to": priorities[c.id]},
            )
            for c in current
            if c.priority != priorities[c.id]
        ]
        await self.repo.reorder(priorities, events, self.now())
        return await self.repo.channels()

    async def request_trial(
        self, channel_id: str, key: DescriptionKey, *, actor: str
    ) -> Trial:
        """排一次试采（worker 去跑）。停用的渠道不试。"""
        channel = await self.get(channel_id)
        if channel.status == "disabled":
            raise SupplyError(409, "channel_disabled", "enable the channel first")
        trial = Trial(
            id=self.new_id(),
            channel_id=channel_id,
            key=key.as_dict(),
            status="queued",
            requested_by=actor,
            created_at=self.now(),
        )
        await self.repo.add_trial(trial)
        return trial

    async def ensure_site(self, url: str) -> Channel:
        """网址落在哪条渠道：已登记的网站（含它的子域名）照那条；没登记的自动建一条
        「待审」的通用网页渠道，需求等审核（0045 第 3.1 节）。"""
        host = site_of(url)
        found = await self.repo.channel_for_host(host)
        if found is not None:
            return found
        now = self.now()
        site = Channel(
            id=site_channel_id(host),
            name=host,
            kind="web",
            plugin="web",
            doc_types=("webpage",),
            readings=("original", "text"),
            license="self",  # 审核时定；没审过的不会被用上
            review="pending",
            rate_per_minute=6,
            daily_cap=200,
            priority=10_000,
            domain=host,
            created_at=now,
            updated_at=now,
        )
        return await self.repo.add_site(
            site, self._event(site.id, "site_found", "system", None, {"url_host": host})
        )

    async def sites(self) -> tuple[SiteSummary, list[dict[str, object]]]:
        """待审网站：每个等了多久、有几项需求在等；连同告警。"""
        rows = await self.repo.pending_sites()
        now = self.now()
        since = [r["since"] for r in rows if r.get("since")]
        summary = SiteSummary(
            pending=len(rows),
            oldest_since=min(since) if since else None,  # type: ignore[type-var]
            now=now,
        )
        return summary, rows

    async def expire_sites(self) -> int:
        """待审超过时限没人审：在等这些网站的需求项转「无法满足」（网站照旧待审，审过以后
        新的需求照取）。回转了几项；待审太多、等太久就记一条告警日志。"""
        now = self.now()
        changed = await self.repo.expire_sites(now - SITE_REVIEW_WITHIN, now)
        summary, _ = await self.sites()
        if summary.alerts:
            log.warning(
                "supply_sites_alert pending=%s alerts=%s",
                summary.pending,
                ",".join(summary.alerts),
            )
        return changed


class TrialRunner:
    """worker 里跑一次试采：列出前几条，取第一条，记大小、类型、摘要。不留存、不交付。"""

    def __init__(
        self,
        repo: CatalogRepository,
        catalogs: dict[str, Catalog],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repo = repo
        self.catalogs = catalogs
        self.now = clock

    async def run(self, trial_id: str) -> str:
        trial = await self.repo.start_trial(trial_id, self.now())
        if trial is None:
            return "skipped"
        channel = await self.repo.get_channel(trial.channel_id)
        catalog = self.catalogs.get(channel.plugin) if channel else None
        if catalog is None:
            await self.repo.finish_trial(
                trial_id, self.now(), status="failed", error="plugin_unavailable"
            )
            return "failed"
        key = DescriptionKey.from_dict(trial.key)
        try:
            entries = await catalog.list(key)
            listed = [
                {
                    "channel_doc_id": e.channel_doc_id,
                    "title": e.title,
                    "url": e.url,
                    "disclosed_on": e.disclosed_on.isoformat()
                    if e.disclosed_on
                    else None,
                    "doc_type": e.doc_type,
                }
                for e in entries[:TRIAL_LISTED]
            ]
            if not entries:
                await self.repo.finish_trial(
                    trial_id, self.now(), status="failed", error="nothing_listed"
                )
                return "failed"
            content, media_type = await catalog.fetch(entries[0])
        except SupplyError as exc:
            await self.repo.finish_trial(
                trial_id, self.now(), status="failed", error=exc.code[:200]
            )
            return "failed"
        except Exception as exc:  # 试采只是给人看的：意外的错误也照实记下
            log.warning(
                "supply_trial_failed trial=%s error=%s", trial_id, type(exc).__name__
            )
            await self.repo.finish_trial(
                trial_id, self.now(), status="failed", error=type(exc).__name__[:200]
            )
            return "failed"
        fetched = {
            "channel_doc_id": entries[0].channel_doc_id,
            "media_type": media_type,
            "size_bytes": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }
        status = "ok" if content else "failed"
        await self.repo.finish_trial(
            trial_id,
            self.now(),
            status=status,
            listed=listed,
            fetched=fetched,
            error=None if content else "empty_content",
        )
        return status


def _check_plugin(reg: Registration) -> None:
    kind = PLUGINS.get(reg.plugin)
    if kind is None:
        raise refused("plugin_unknown", f"plugins: {', '.join(sorted(PLUGINS))}")
    if kind != reg.kind:
        raise refused(
            "plugin_kind_mismatch", f"plugin {reg.plugin} is a {kind} adapter"
        )


def _need_reason(reason: str) -> None:
    if not reason.strip():
        raise refused("reason_required", "say why")


def _shape(channel: Channel) -> dict[str, object]:
    return {
        "name": channel.name,
        "doc_types": list(channel.doc_types),
        "readings": list(channel.readings),
        "license": channel.license,
        "rate_per_minute": channel.rate_per_minute,
        "daily_cap": channel.daily_cap,
        "domain": channel.domain,
        "terms_url": channel.terms_url,
        "render_allowed": channel.render_allowed,
        "obey_robots": channel.obey_robots,
        "config": channel.config,
    }
