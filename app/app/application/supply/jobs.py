"""作业的执行（SDD 0045 第 3.3–3.5 节、第四节）：列 → 取 → 留存 → 交 knowbase → 加工 → 回执；
给网址：取这一页（和它直接链到的文件）→ 留存 → 交付；订阅的「看新的」、按主题的「搜」；
上传资料的加工作业：领加工授权 → 读原件 → 加工 → 写回读法。

一个取数作业跨用户共享：同一渠道同一描述（或同一网址）只取一次，等它的需求项都挂在上面。
作业由调度（`scheduler.py`）按渠道、四档、按用户轮流排出去；插件每问一次渠道都经限速。

- 尝试：每次执行记开始、结束、结果、原因。临时出错（对方 5xx、超时、knowbase 暂时
  不可用）按逐次拉长的间隔重试，有次数上限；渠道这会儿不能再问（日上限用完）或对方回 429
  限流，放回队列、不算一次失败（0045 第 3.3 节「被限流等到能取再取」，独立审第 9 条）；
  放回与结束这次尝试在同一个事务里（独立审第 10 条）。
- **取不到换下一个**（0045 第四节）：渠道明说没有（列出来是空的、对方回 4xx），或这条渠道
  取不成（重试用完），在等的需求项挂到来源目录里下一个能供、且满足确认时供给条件的渠道；
  都试过了才结束：渠道里都没有转「无法满足」（`channel_has_none`），有取不成的转「失败」。
- **渠道失败只算对方的错**（独立审第 3 条）：对方 5xx、超时、取数失败、回包格式不对，才计
  连续失败（到次数自动暂停）；4xx 是「这一条没有」，不计。
- 作业结束时还有等它的需求项（跑的中途才挂上来的），作业放回队列再跑一次，不让它们停在
  「取数中」（独立审第 2 条，在记录那头同一个事务里核）。
- 交付可安全重试：knowbase 按渠道编号与内容摘要认同一份，引用按需求依据只记一条；
  披露时间窗里以前交过的那一份不再重取，只给新来的需求记引用。knowbase 说「这一版以前交过」
  时，读法以那次实际做成的（原件记录里记的）为准，没有记录就重新领授权加工（独立审第 1 条）。
- 加工没成功原件照样交付，需求项照实写（只要原件的不因加工失败降级）。读法按内容定：
  PDF 按页文字，定期报告全文再抽三张合并报表；网页、文本、Markdown 写正文（`readings.py`）。
  这几样都是吃 CPU 的同步活，放到线程里跑，不堵事件循环（独立审第 5 条）。加工授权用完就交还
  （独立审第 11 条）。
- 原件交 knowbase 拿到回执后再留 30 天，到期删内容（`retention.py`）；取到了没交付成的，
  从取到时起也只留 30 天（独立审第 13 条）。
- 渠道（0045 第 3.1 节）：渠道暂停、改了登记等重新审核时作业不跑、等着；停用或审不过的，
  在等的需求项转「无法满足」。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import unquote, urlsplit

from app.application.ports.supply import (
    Catalog,
    Knowbase,
    KnowbaseReader,
    ObjectStore,
    SupplyRepository,
)
from app.application.supply.processing import (
    FACTS_VERSION,
    PAGES_VERSION,
    REPORT_SHAPES,
    PdfPages,
    PdfWords,
    pages_reading,
    report_statements,
    wanted_readings,
)
from app.application.supply.readings import (
    TEXT_VERSION,
    form_of,
    recognize_periodic,
    text_reading,
)
from app.domain.supply.catalog import AUTO_PAUSE_AFTER, web_doc_id
from app.domain.supply.models import (
    MAX_ATTEMPTS,
    REASON_AWAITING_KNOWBASE,
    REASON_CHANNEL_DISABLED,
    REASON_CHANNEL_HAS_NONE,
    REASON_OFF_SITE,
    REASON_READINGS_INCOMPLETE,
    REASON_RENDER_UNAVAILABLE,
    REASON_ROBOTS,
    REASON_SUBJECT_REQUIRED,
    REASON_UNEXPECTED_RESPONSE,
    REASON_URL_REJECTED,
    Channel,
    Demand,
    DemandItem,
    DescriptionKey,
    Entry,
    Job,
    Original,
    OriginalDelivery,
    Reference,
    SupplyError,
    basis_of,
)
from app.domain.supply.schedule import (
    ChannelDeferred,
    Conditions,
    candidates,
    demand_tier,
)
from app.domain.supply.vocabulary import is_periodic

log = logging.getLogger(__name__)

RETENTION = timedelta(days=30)
_BACKOFF = (
    timedelta(minutes=1),
    timedelta(minutes=5),
    timedelta(minutes=30),
    timedelta(hours=2),
)
# 来源回 429 时放回队列等多久（不算一次尝试）
RATE_LIMITED_WAIT = timedelta(minutes=10)
# 财报抽表：四类定期报告的三张合并报表（10-28 起半年报、季报也抽，见 processing）
FACT_DOC_TYPES = tuple(REPORT_SHAPES)
# 交 knowbase 的文件名后缀按内容类型（knowbase 按后缀认类型、再核文件头）
_SUFFIX = {
    "application/pdf": ".pdf",
    "text/plain": ".txt",
    "text/markdown": ".md",
    "text/html": ".html",
    "application/xhtml+xml": ".html",
    "text/csv": ".csv",
    "application/vnd.ms-excel": ".xls",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
}
# 这几种「对方拒了」本身就是原因，照它写进需求项；别的 4xx 都是「渠道里没有这一份」
_OWN_REASONS = frozenset(
    {REASON_ROBOTS, REASON_OFF_SITE, REASON_RENDER_UNAVAILABLE, REASON_URL_REJECTED}
)
PUBLIC_OWNER = "public:disclosure"

Plugins = Callable[[Channel], Catalog | None]
Ask = Callable[[Channel, Awaitable[Any]], Awaitable[Any]]


class ChannelAbsent(Exception):  # noqa: N818 - 不是出错，是渠道答了「没有」
    """渠道答了 4xx（不是限流）：这一条没有，不计渠道失败。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class RoundWork(Protocol):
    """订阅的「看新的」、按主题的「搜」：作业里跑一轮（`subscriptions.py`、`searches.py`）。"""

    async def run(self, job: Job, channel: Channel, adapter: Any, ask: Ask) -> str: ...

    async def give_up(self, job: Job, reason: str) -> None: ...


class JobRunner:
    def __init__(
        self,
        repo: SupplyRepository,
        plugins: Plugins,
        knowbase: Knowbase,
        objects: ObjectStore,
        *,
        pdf_pages: PdfPages,
        pdf_words: PdfWords,
        reader: KnowbaseReader | None = None,
        watches: RoundWork | None = None,
        searches: RoundWork | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self.repo = repo
        self.plugins = plugins
        self.knowbase = knowbase
        self.objects = objects
        self.pdf_pages = pdf_pages
        self.pdf_words = pdf_words
        self.reader = reader
        self.watches = watches
        self.searches = searches
        self.now = clock
        self.new_id = new_id

    async def run(self, job_id: str, token: str | None = None) -> str:
        """跑一次；返回这次的结果（done、absent、moved、retry、failed、held、deferred、
        withdrawn、skipped）。`token` 是调度派发时的代次（随投递消息带）：同一作业只让拿着
        这一代、且没有没结束的尝试的 worker 开跑（二审第 6 条）。"""
        job = await self.repo.get_job(job_id)
        if job is None or job.status not in ("queued", "waiting", "running"):
            return "skipped"
        if job.kind == "process":
            return await self._run_process(job, token)
        channel = await self.repo.get_channel(job.channel_id)
        if (
            channel is not None
            and job.kind != "search"
            and (channel.status == "paused" or channel.review == "pending")
        ):
            # 暂停、改了登记等重新审核：取数、看新的作业等着，恢复或重新放行后接着排；
            # 搜索作业（search）有时效与并发上限，不在暂停/待审渠道上无限挂起（三审第 5 条）
            if not await self.repo.hold_job(job_id, token=token):
                return "skipped"
            return "held"
        if (
            channel is None
            or channel.status == "disabled"
            or channel.review != "approved"
            or (job.kind == "search" and channel.status != "active")
        ):
            # 停用、审不过（撤销）的渠道，或暂停/待重审渠道上的搜索作业：这里收口
            attempt = await self.repo.start_attempt(job_id, self.now(), token=token)
            if attempt is None:
                return "skipped"
            await self._end_job(job, "unsatisfiable", REASON_CHANNEL_DISABLED)
            await self.repo.finish_attempt(
                job_id,
                attempt,
                self.now(),
                outcome="withdrawn",
                reason=REASON_CHANNEL_DISABLED,
                job_status="failed",
                token=token,
            )
            return "withdrawn"
        attempt = await self.repo.start_attempt(job_id, self.now(), token=token)
        if attempt is None:
            return "skipped"
        job = await self.repo.get_job(job_id)
        assert job is not None
        try:
            if job.kind == "watch":
                outcome = await self._round(self.watches, job, channel)
            elif job.kind == "search":
                outcome = await self._round(self.searches, job, channel)
            else:
                outcome = await self._fetch_and_deliver(job, channel)
        except ChannelDeferred as deferred:
            return await self._defer(
                job, attempt, deferred.until, deferred.reason, token=token
            )
        except ChannelAbsent as absent:
            # 渠道答了「这一条没有」：取数的换下一个渠道，都没有才「无法满足」
            reason = (
                absent.reason
                if absent.reason in _OWN_REASONS
                else REASON_CHANNEL_HAS_NONE
            )
            if job.kind == "fetch":
                moved = await self._give_up(job, channel, "unsatisfiable", reason)
                outcome = "moved" if moved else "absent"
                await self._finish(job, attempt, outcome, reason, "done", token=token)
                return outcome
            await self._round_failed(job, reason)
            await self._finish(job, attempt, "failed", reason, "failed", token=token)
            return "failed"
        except SupplyError as exc:
            if exc.status == 429:
                return await self._defer(
                    job,
                    attempt,
                    self.now() + RATE_LIMITED_WAIT,
                    exc.code,
                    token=token,
                )
            if exc.code in (REASON_AWAITING_KNOWBASE, REASON_SUBJECT_REQUIRED):
                # 为用户采集的路 knowbase 还没上：不交付（绝不改走公开原件层）；法定披露
                # 没有主体：交不进公开原件层。都照实结束，不换渠道、不重试
                await self._settle_waiting(
                    job.id,
                    "unsatisfiable"
                    if exc.code == REASON_AWAITING_KNOWBASE
                    else "failed",
                    reason=exc.code,
                )
                await self._finish(
                    job, attempt, "failed", exc.code, "failed", token=token
                )
                return "failed"
            if exc.status >= 500 and job.attempts < MAX_ATTEMPTS:
                return await self._retry(job, attempt, exc.code, token=token)
            await self._fail(job, channel, exc.code)
            await self._finish(job, attempt, "failed", exc.code, "failed", token=token)
            return "failed"
        except Exception as exc:  # 意料之外的：当临时出错重试，次数用完转失败
            log.exception("supply_job_failed job=%s", job_id)
            reason = type(exc).__name__
            if job.attempts < MAX_ATTEMPTS:
                return await self._retry(job, attempt, reason, token=token)
            await self._fail(job, channel, reason)
            await self._finish(job, attempt, "failed", reason, "failed", token=token)
            return "failed"
        await self._finish(job, attempt, outcome, None, "done", token=token)
        return outcome

    async def _finish(
        self,
        job: Job,
        attempt: str,
        outcome: str,
        reason: str | None,
        status: str,
        *,
        token: str | None = None,
    ) -> None:
        await self.repo.finish_attempt(
            job.id,
            attempt,
            self.now(),
            outcome=outcome,
            reason=reason,
            job_status=status,
            token=token,
        )

    async def _retry(
        self,
        job: Job,
        attempt: str,
        reason: str,
        *,
        token: str | None = None,
    ) -> str:
        await self.repo.finish_attempt(
            job.id,
            attempt,
            self.now(),
            outcome="retry",
            reason=reason,
            job_status="waiting",
            not_before=self.now()
            + _BACKOFF[min(max(job.attempts, 1), len(_BACKOFF)) - 1],
            token=token,
        )
        return "retry"

    async def _defer(
        self,
        job: Job,
        attempt: str,
        until: datetime,
        reason: str,
        *,
        token: str | None = None,
    ) -> str:
        """放回队列，`until` 以后再排，不算一次尝试（尝试次数退回去）。"""
        await self.repo.finish_attempt(
            job.id,
            attempt,
            self.now(),
            outcome="deferred",
            reason=reason,
            job_status="waiting",
            not_before=until,
            refund=True,
            token=token,
        )
        return "deferred"

    async def _fail(self, job: Job, channel: Channel, reason: str) -> None:
        if job.kind == "fetch":
            await self._give_up(job, channel, "failed", reason)
        else:
            await self._round_failed(job, reason)

    async def _round(self, work: RoundWork | None, job: Job, channel: Channel) -> str:
        if work is None:
            raise SupplyError(503, "round_not_configured", "no round runner")
        adapter = self.plugins(channel)
        if adapter is None:
            raise SupplyError(503, "plugin_unavailable", "no plugin for the channel")
        return await work.run(job, channel, adapter, self._ask_channel)

    async def _round_failed(self, job: Job, reason: str) -> None:
        work = self.watches if job.kind == "watch" else self.searches
        if work is not None:
            await work.give_up(job, reason)

    async def _end_job(self, job: Job, status: str, reason: str) -> None:
        if job.kind == "fetch":
            await self._settle_waiting(job.id, status, reason=reason)
        else:
            await self._round_failed(job, reason)

    # —— 换下一个渠道 ——

    async def _give_up(
        self, job: Job, channel: Channel, status: str, reason: str
    ) -> int:
        """这条渠道取不到：在等的需求项挂到下一个能供、满足供给条件的渠道；没有下一个的
        转 `status`（附 `reason`）。回换了几项。给网址的项只有它那个网站一条渠道。"""
        waiting = await self.repo.waiting_items(job.id)
        if not waiting:
            return 0
        channels = await self.repo.channels()
        moves: list[tuple[str, Job, tuple[str, ...], str]] = []
        ended: dict[tuple[str, str], list[str]] = {}
        for item, demand in waiting:
            tried = tuple(dict.fromkeys((*item.tried_channels, channel.id)))
            following = _next_channel(channels, item, demand, tried)
            if following is None or item.key is None:
                # 前面哪条渠道取不成的，结局是「失败」；都只是没有，才是「渠道里没有」
                final = (status, reason)
                if status == "unsatisfiable" and item.reason not in (
                    None,
                    REASON_CHANNEL_HAS_NONE,
                    *_OWN_REASONS,
                ):
                    final = ("failed", str(item.reason))
                ended.setdefault(final, []).append(item.id)
                continue
            moves.append(
                (
                    item.id,
                    Job(
                        id=self.new_id(),
                        kind="fetch",
                        channel_id=following.id,
                        dedup_key=f"fetch:{following.id}:{item.key.text}",
                        payload={"key": item.key.as_dict()},
                        status="queued",
                        attempts=0,
                        created_at=self.now(),
                        tier=demand_tier(demand.proposer, demand.app),
                    ),
                    tried,
                    # 记下这条渠道为什么没取到：最后都没取到时据此定「失败」还是「没有」
                    reason
                    if status == "failed" or item.reason is None
                    else str(item.reason),
                )
            )
        await self.repo.move_items(moves, self.now())
        for (final_status, final_reason), ids in ended.items():
            await self.repo.settle_items(
                ids, final_status, self.now(), reason=final_reason
            )
        return len(moves)

    # —— 问渠道 ——

    async def _ask_channel(self, channel: Channel, call: Awaitable[Any]) -> Any:
        """问渠道（列、取、看新的、搜）。对方的错（5xx、超时、取数失败、回包格式不对）计一次
        渠道失败；4xx 是「这一条没有」（抛 `ChannelAbsent`，不计）；限流、等名额都不算。
        答了就把连续失败清零。"""
        try:
            answer = await call
        except ChannelDeferred:
            raise
        except SupplyError as exc:
            if exc.status == 429:
                raise
            if exc.status >= 500:
                await self._channel_failed(channel)
                raise
            raise ChannelAbsent(exc.code) from None
        except Exception:
            await self._channel_failed(channel)
            raise
        await self.repo.channel_succeeded(channel.id)
        return answer

    async def _channel_failed(self, channel: Channel) -> None:
        paused = await self.repo.channel_failed(
            channel.id, self.now(), pause_after=AUTO_PAUSE_AFTER
        )
        if paused:
            # 告警：日志按这个事件名报警（0045 第 3.1 节「自动暂停并告警」）
            log.warning(
                "supply_channel_auto_paused channel=%s failures=%s",
                channel.id,
                AUTO_PAUSE_AFTER,
            )

    # —— 取数作业 ——

    async def _fetch_and_deliver(self, job: Job, channel: Channel) -> str:
        plugin = self.plugins(channel)
        if plugin is None:
            raise SupplyError(503, "plugin_unavailable", "no plugin for the channel")
        if job.payload.get("url"):
            return await self._deliver_url(job, channel, plugin)
        raw_key = job.payload.get("key")
        key = DescriptionKey.from_dict(raw_key) if isinstance(raw_key, dict) else None
        raw_entry = job.payload.get("entry")
        if isinstance(raw_entry, dict):
            # 订阅这一轮已经看到了这一份：不必再「列」
            entries = [Entry.from_dict(raw_entry)]
        else:
            assert key is not None
            entries = await self._ask_channel(channel, plugin.list(key))
        if not entries:
            # 渠道明说没有（报告还没披露、这家没有这一类、窗里没有这类公告）：不重试，
            # 换下一个渠道；都没有才转「无法满足」
            moved = await self._give_up(
                job, channel, "unsatisfiable", REASON_CHANNEL_HAS_NONE
            )
            return "moved" if moved else "absent"
        # 定期报告：新的在前，更正版是同一份资料的新版本，交最新那一条
        periodic = key is not None and is_periodic(key.doc_type)
        chosen = entries[:1] if periodic else entries
        waiting = await self.repo.waiting_items(job.id)
        parts: dict[str, list[dict[str, object]]] = {i.id: [] for i, _ in waiting}
        readings: set[str] | None = None
        for entry in chosen:
            got, done = await self._deliver_one(channel, plugin, entry, key, waiting)
            for item_id, part in got.items():
                parts.setdefault(item_id, []).append(part)
            readings = done if readings is None else readings & done
        await self._settle_delivered(waiting, readings or set(), parts)
        return "done"

    async def _settle_delivered(
        self,
        waiting: list[tuple[DemandItem, Demand]],
        readings: set[str],
        parts: dict[str, list[dict[str, object]]],
        extra: dict[str, object] | None = None,
    ) -> None:
        """每一项照自己的回执结算（为用户采集的，每人拿到的是自己名下那一份）。超时的项
        状态不再前进，回执照样记上（0045 第 2.5 节「超时后数据到了照样进用户的栏」）。
        若某需求项未拿到任何已交付资料（knowbase 回包漏了该受益人），按失败收口（三审第 11 条）。"""
        for item, _ in waiting:
            materials = parts.get(item.id) or []
            if not materials:
                await self.repo.settle_items(
                    [item.id],
                    "failed",
                    self.now(),
                    reason=REASON_UNEXPECTED_RESPONSE,
                )
                continue
            receipt: dict[str, object] = {"materials": materials, **(extra or {})}
            if len(materials) == 1:
                receipt.update(materials[0])
            item_readings = set(readings)
            mat_readings = [
                set(str(r) for r in m["readings"])
                for m in materials
                if isinstance(m.get("readings"), list)
            ]
            if mat_readings:
                item_readings = set.intersection(*mat_readings)
            status = wanted_readings(item.readings, item_readings)
            await self.repo.settle_items(
                [item.id],
                status,
                self.now(),
                receipt=receipt,
                reason=None if status == "ready" else REASON_READINGS_INCOMPLETE,
            )

    def _web_entry(
        self, channel: Channel, url: str, title: str, doc_type: str
    ) -> Entry:
        """网页、链到的文件交 knowbase 时的描述：按渠道配置写，不用任何人的预填（为用户采集
        的，每人的预填只用在他自己那份，见 `_beneficiaries`）。"""
        configured = channel.config.get("doc_type")
        subject = channel.config.get("subject")
        return Entry(
            channel_id=channel.id,
            channel_doc_id=web_doc_id(url),
            title=title[:200] or url,
            url=url,
            disclosed_on=None,
            doc_type=configured
            if isinstance(configured, str) and configured
            else doc_type,
            subject=subject if isinstance(subject, str) and subject else None,
        )

    async def _deliver_url(self, job: Job, channel: Channel, plugin: Any) -> str:
        """给网址（0045 第 2.2 节）：取这一页；要连文件的，同一网站、只往下一层，最多 20 份，
        超出的写进回执「还有几个没取」。连到的文件哪个取不到，记下来，不挡这一页。"""
        url = str(job.payload["url"])
        content, media_type, final = await self._ask_channel(
            channel, plugin.fetch_url(url)
        )
        waiting = await self.repo.waiting_items(job.id)
        is_html = form_of(media_type) == "html"
        default_type = "webpage" if is_html else _linked_type(media_type)
        filename_fn = getattr(plugin, "filename_of", None)
        disposition_name = (
            filename_fn(final) or filename_fn(url) if callable(filename_fn) else None
        )
        default_title = url if is_html else _url_title(final or url, disposition_name)
        entry = self._web_entry(channel, url, default_title, default_type)
        got, readings = await self._deliver_content(
            channel, entry, content, media_type, None, waiting
        )
        parts = {item_id: [part] for item_id, part in got.items()}
        extra: dict[str, object] = {}
        if job.payload.get("follow_links") and is_html:
            files, rest = plugin.linked_files(content, final)
            failed: list[dict[str, str]] = []
            for link, text in files:
                try:
                    body, kind, final_link = await self._ask_channel(
                        channel, plugin.fetch_url(link, file=True)
                    )
                except ChannelAbsent as exc:
                    failed.append({"url": link, "reason": exc.reason})
                    continue
                except SupplyError as exc:
                    if exc.status == 429:
                        raise
                    failed.append({"url": link, "reason": exc.code})
                    continue
                link_name = (
                    filename_fn(final_link) or filename_fn(link)
                    if callable(filename_fn)
                    else None
                )
                link_title = (
                    text
                    if text and text != link
                    else _url_title(final_link or link, link_name)
                )
                linked = self._web_entry(channel, link, link_title, _linked_type(kind))
                more, _ = await self._deliver_content(
                    channel, linked, body, kind, None, waiting
                )
                for item_id, part in more.items():
                    parts.setdefault(item_id, []).append(part)
            extra["not_taken"] = rest
            if failed:
                extra["failed_links"] = failed
        await self._settle_delivered(waiting, readings, parts, extra)
        return "done"

    async def _deliver_one(
        self,
        channel: Channel,
        plugin: Catalog,
        entry: Entry,
        key: DescriptionKey | None,
        waiting: list[tuple[DemandItem, Demand]],
    ) -> tuple[dict[str, dict[str, object]], set[str]]:
        """取一份、留存、交 knowbase、加工；回（各项回执里的这一份，就绪的读法）。"""
        if channel.zone == "disclosure" and not is_periodic(entry.doc_type):
            # 窗里的每条公告、问答是一份不变的公开资料（更正另是一条）：交过的不再重取，
            # 只给新来的需求记引用。为用户采集的每人各得一份，不走这条（三审第 1 条）
            earlier = await self.repo.delivered_original(
                entry.channel_id, entry.channel_doc_id
            )
            if earlier is not None:
                original, delivery = earlier
                references = [
                    Reference(d.owner, i.basis_ref, basis_of(d, i)) for i, d in waiting
                ]
                if references:
                    await self.knowbase.add_references(
                        delivery.knowbase_material_id,
                        delivery.knowbase_version_no,
                        references,
                    )
                done = set(delivery.readings) | {"original"}
                part, done = self._receipt(
                    entry,
                    original,
                    delivery.knowbase_material_id,
                    delivery.knowbase_version_no,
                    done,
                )
                return {i.id: part for i, _ in waiting}, done
        content, media_type = await self._ask_channel(channel, plugin.fetch(entry))
        return await self._deliver_content(
            channel, entry, content, media_type, key, waiting
        )

    async def _deliver_content(
        self,
        channel: Channel,
        entry: Entry,
        content: bytes,
        media_type: str,
        key: DescriptionKey | None,
        waiting: list[tuple[DemandItem, Demand]],
    ) -> tuple[dict[str, dict[str, object]], set[str]]:
        """按渠道审核定的「区」交 knowbase（0045 第 3.5 节、0043 第 2.1、2.4 节）：
        - 法定披露（`disclosure`）：进公开原件层，描述按渠道给的（目录的条目、渠道配置），
          给每条需求依据记引用；没有主体的交不进去，这一份照实失败；
        - 为用户采集（`collected`）：走 knowbase「为某用户采集」那条路，内容存一份，每位
          受益人各得一份自己名下的资料，描述用他自己的预填；那条路没上之前不交付。
        回（各项回执里的这一份，就绪的读法）。"""
        original = await self._keep(entry, content, media_type)
        name = _file_name(entry, media_type)
        facts = (
            key is not None
            and entry.doc_type in FACT_DOC_TYPES
            and entry.edition == "full"
        )
        now = self.now()
        if channel.zone == "disclosure":
            periodic = key is not None and is_periodic(entry.doc_type)
            subject = key.subject if key is not None else entry.subject
            if not subject:
                raise SupplyError(422, REASON_SUBJECT_REQUIRED, "no subject to file")
            earlier_pub = await self.repo.delivered_original(
                entry.channel_id, entry.channel_doc_id
            )
            delivered = await self.knowbase.deliver_public(
                channel=entry.channel_id,
                channel_doc_id=entry.channel_doc_id,
                name=name,
                content=content,
                subject=subject,
                doc_type=entry.doc_type,
                period_start=key.period_start if periodic and key else None,
                period_end=key.period_end if periodic and key else None,
                edition=entry.edition,
                topics=entry.topics,
                disclosed_on=entry.disclosed_on.isoformat()
                if entry.disclosed_on
                else None,
                source_url=entry.url,
            )
            material_id = str(delivered["material_id"])
            version_no = int(delivered["version_no"])
            created = bool(delivered.get("created"))
            done = {"original"}
            recorded: set[str] | None = None
            if (
                earlier_pub is not None
                and earlier_pub[1].knowbase_material_id == material_id
                and earlier_pub[1].knowbase_version_no == version_no
                and earlier_pub[1].readings
            ):
                recorded = set(earlier_pub[1].readings)
            if created or recorded is None:
                grant = await self.knowbase.processing_grant(
                    material_id, version_no, owner=PUBLIC_OWNER
                )
                try:
                    done |= await self._process(
                        grant,
                        material_id,
                        content,
                        media_type,
                        key if facts else None,
                        title=entry.title,
                        published_on=entry.disclosed_on,
                    )
                finally:
                    await self._release(grant)
            else:
                done |= recorded
            references = [
                Reference(d.owner, i.basis_ref, basis_of(d, i)) for i, d in waiting
            ]
            if references:
                await self.knowbase.add_references(material_id, version_no, references)
            await self.repo.mark_delivered(
                original.id,
                now,
                now + RETENTION,
                [
                    OriginalDelivery(
                        id=self.new_id(),
                        original_id=original.id,
                        channel_id=entry.channel_id,
                        channel_doc_id=entry.channel_doc_id,
                        zone="disclosure",
                        owner_subject=PUBLIC_OWNER,
                        knowbase_material_id=material_id,
                        knowbase_version_no=version_no,
                        readings=tuple(sorted(done)),
                        delivered_at=now,
                    )
                ],
            )
            part, done = self._receipt(entry, original, material_id, version_no, done)
            return {i.id: part for i, _ in waiting}, done

        answered = await self.knowbase.deliver_collected(
            channel=entry.channel_id,
            channel_doc_id=entry.channel_doc_id,
            name=name,
            content=content,
            license=channel.license,
            source_url=entry.url,
            beneficiaries=_beneficiaries(waiting),
            default_description=_default_description(entry, key),
        )
        if not answered:
            raise SupplyError(502, "unexpected_response", "no collected materials")
        by_ref = {
            str(a.get("item_ref")): a for a in answered if a.get("item_ref") is not None
        }
        by_subject: dict[str, list[dict[str, Any]]] = {}
        for a in answered:
            subj = str(a.get("subject") or "")
            by_subject.setdefault(subj, []).append(a)
        materials: dict[str, dict[str, Any]] = {}
        for item, demand in waiting:
            found = by_ref.get(item.basis_ref)
            if found is None:
                candidates_for_owner = by_subject.get(demand.owner) or []
                if len(candidates_for_owner) == 1:
                    found = candidates_for_owner[0]
            if (
                found is not None
                and found.get("material_id") is not None
                and found.get("version_no") is not None
            ):
                materials[item.id] = found

        # 判断哪些受益人副本已有读法、哪些还需要加工（三审第 2 条）
        needs_processing: list[dict[str, Any]] = []
        copy_readings: dict[str, set[str]] = {}
        for a in answered:
            mid = str(a.get("material_id") or "")
            given_readings = a.get("readings")
            has_readings = a.get("has_readings")
            if isinstance(given_readings, list) and given_readings:
                copy_readings[mid] = {str(r) for r in given_readings} | {"original"}
            elif has_readings is True:
                copy_readings[mid] = _expected_readings(media_type, facts)
            elif bool(a.get("created")) or has_readings is False:
                needs_processing.append(a)
            else:
                needs_processing.append(a)

        processed_done = {"original"}
        if needs_processing:
            # 按受益人顺序尝试领授权：若第一位受益人已撤回资料/拒绝授权（4xx），换下一位重试
            order = [
                *needs_processing,
                *[a for a in answered if a not in needs_processing],
            ]
            for cand in order:
                cand_mid = str(cand["material_id"])
                cand_vno = int(cand["version_no"])
                cand_owner = str(cand.get("subject") or "")
                try:
                    grant = await self.knowbase.processing_grant(
                        cand_mid, cand_vno, owner=cand_owner
                    )
                except SupplyError as exc:
                    if exc.status in (403, 404, 409, 422):
                        log.warning(
                            "supply_collected_grant_skipped owner=%s material=%s code=%s",
                            cand_owner,
                            cand_mid,
                            exc.code,
                        )
                        continue
                    raise
                try:
                    processed_done |= await self._process(
                        grant,
                        cand_mid,
                        content,
                        media_type,
                        key if facts else None,
                        title=entry.title,
                        published_on=entry.disclosed_on,
                    )
                finally:
                    await self._release(grant)
                for a in answered:
                    amid = str(a.get("material_id") or "")
                    copy_readings[amid] = set(processed_done)
                break

        deliveries: list[OriginalDelivery] = []
        seen_owners: set[str] = set()
        for a in answered:
            amid = str(a.get("material_id") or "")
            avno = int(a.get("version_no") or 1)
            aowner = str(a.get("subject") or "")
            if not amid or not aowner or aowner in seen_owners:
                continue
            seen_owners.add(aowner)
            rset = copy_readings.get(amid, processed_done)
            deliveries.append(
                OriginalDelivery(
                    id=self.new_id(),
                    original_id=original.id,
                    channel_id=entry.channel_id,
                    channel_doc_id=entry.channel_doc_id,
                    zone="collected",
                    owner_subject=aowner,
                    knowbase_material_id=amid,
                    knowbase_version_no=avno,
                    readings=tuple(sorted(rset)),
                    delivered_at=now,
                )
            )
        await self.repo.mark_delivered(
            original.id,
            now,
            now + RETENTION,
            deliveries,
        )
        parts: dict[str, dict[str, object]] = {}
        all_done: set[str] | None = None
        for item_id, found in materials.items():
            mid = str(found["material_id"])
            vno = int(found["version_no"])
            item_done = copy_readings.get(mid, processed_done)
            parts[item_id] = self._receipt(entry, original, mid, vno, item_done)[0]
            all_done = item_done if all_done is None else all_done & item_done
        return parts, all_done or processed_done

    @staticmethod
    def _receipt(
        entry: Entry,
        original: Original,
        material_id: str,
        version_no: int,
        done: set[str],
    ) -> tuple[dict[str, object], set[str]]:
        return (
            {
                "material_id": str(material_id),
                "version_no": int(version_no or 1),
                "sha256": original.sha256,
                "readings": sorted(done),
                "channel_doc_id": entry.channel_doc_id,
                "doc_type": entry.doc_type,
            },
            done,
        )

    async def _release(self, grant: str) -> None:
        """加工授权用完交还（凭授权读的关系没配时交还不了，授权 3 天后自己过期）。"""
        if self.reader is None:
            return
        try:
            await self.reader.release(grant)
        except Exception as exc:  # 交还失败不影响结果
            log.warning("supply_grant_release_failed error=%s", type(exc).__name__)

    # —— 加工 ——

    async def _process(
        self,
        grant: str,
        material_id: str,
        content: bytes,
        media_type: str,
        facts_key: DescriptionKey | None,
        *,
        recognize: bool = False,
        title: str | None = None,
        published_on: date | None = None,
    ) -> set[str]:
        """凭加工授权写回读法（0045 第 3.4 节）：PDF 写按页文字，`facts_key` 给了（定期报告
        全文）再抽三张合并报表；网页、文本、Markdown 写正文；表格还没有写回格式。没成的
        照实写原因。回写成了哪几种。"""
        form = form_of(media_type)
        done: set[str] = set()
        if form == "pdf":
            pages = await self._write_pages(grant, material_id, content)
            if pages is not None:
                done.add("pages")
            if recognize and facts_key is None and pages is not None:
                # 上传的 PDF 没有渠道给的描述：从封面认是不是定期报告全文
                facts_key = recognize_periodic(pages)
            if facts_key is not None and await self._write_facts(
                grant, material_id, content, facts_key
            ):
                done.add("table")
            return done
        if form in ("html", "text"):
            try:
                reading = await asyncio.to_thread(
                    text_reading,
                    content,
                    media_type,
                    title=title,
                    published_on=published_on,
                )
            except (ValueError, UnicodeDecodeError) as exc:
                log.warning("supply_text_failed material=%s error=%s", material_id, exc)
                await self.knowbase.write_failed(
                    grant, "text", TEXT_VERSION, "no_text_found"
                )
                return done
            await self.knowbase.write_text(
                grant,
                TEXT_VERSION,
                reading.body,
                title=reading.title,
                published_on=reading.published_on,
            )
            done.add("text")
            return done
        # Excel、CSV：表格整理还没有写回格式（knowbase 的表格读法只收财报事实行）；
        # 别的认不得的内容：照实记「没成」，免得一直留在待加工清单里
        kind, reason = (
            ("table", "table_format_not_supported")
            if form == "table"
            else ("text", "unsupported_media_type")
        )
        await self.knowbase.write_failed(grant, kind, TEXT_VERSION, reason)
        return done

    async def _write_pages(
        self, grant: str, material_id: str, pdf: bytes
    ) -> list[dict[str, object]] | None:
        try:
            pages = await asyncio.to_thread(pages_reading, pdf, self.pdf_pages)
            await self.knowbase.write_pages(grant, PAGES_VERSION, pages)
            return pages
        except SupplyError:
            raise
        except Exception as exc:
            log.warning("supply_pages_failed material=%s error=%s", material_id, exc)
            await self.knowbase.write_failed(
                grant, "pages", PAGES_VERSION, "unreadable"
            )
            return None

    async def _write_facts(
        self, grant: str, material_id: str, pdf: bytes, key: DescriptionKey
    ) -> bool:
        try:
            facts = await asyncio.to_thread(report_statements, pdf, key, self.pdf_words)
        except Exception as exc:
            log.warning("supply_facts_failed material=%s error=%s", material_id, exc)
            await self.knowbase.write_failed(
                grant, "table", FACTS_VERSION, "unreadable"
            )
            return False
        if not facts.raw:
            reason = ";".join(facts.problems)[:450] or "no statements found"
            await self.knowbase.write_failed(grant, "table", FACTS_VERSION, reason)
            return False
        await self.knowbase.write_facts(grant, FACTS_VERSION, facts.raw, facts.standard)
        return True

    # —— 上传资料的加工作业 ——

    async def _run_process(self, job: Job, token: str | None = None) -> str:
        """上传资料的加工（0045 第 3.4 节）：凭本人的加工依据领一条只这一版、只这个目的
        的加工授权，经 knowbase 接口读原件（不碰桶、不留存），加工，写回读法，交还授权。
        资料在加工中被撤销、清除的，knowbase 拒绝写回，作业照实结束。

        彻底失败（4xx、重试用完）时写一条「没成」的读法，免得 knowbase 的待加工清单一直
        列着它、每分钟又建一个作业（独立审第 7 条）。"""
        attempt = await self.repo.start_attempt(job.id, self.now(), token=token)
        if attempt is None:
            return "skipped"
        job = await self.repo.get_job(job.id) or job
        owner = str(job.payload["owner"])
        material_id = str(job.payload["material_id"])
        version_no = int(job.payload["version_no"])  # type: ignore[arg-type]
        grant: str | None = None
        try:
            if self.reader is None:
                raise SupplyError(
                    503, "reader_not_configured", "user-data-read is not set"
                )
            grant = await self.knowbase.processing_grant(
                material_id, version_no, owner=owner
            )
            content, media_type = await self.reader.content(
                grant, material_id, version_no
            )
            done = await self._process(
                grant, material_id, content, media_type, None, recognize=True
            )
        except Exception as exc:
            code = exc.code if isinstance(exc, SupplyError) else type(exc).__name__
            temporary = not isinstance(exc, SupplyError) or (
                exc.status >= 500 or exc.status == 429
            )
            if not isinstance(exc, SupplyError):
                log.exception("supply_process_failed job=%s", job.id)
            if temporary and job.attempts < MAX_ATTEMPTS:
                if grant is not None:
                    await self._release(grant)
                return await self._retry(job, attempt, code, token=token)
            if grant is not None:
                try:
                    await self.knowbase.write_failed(
                        grant, "text", TEXT_VERSION, f"processing_failed:{code}"[:200]
                    )
                except Exception as written:  # 写不回去（授权已撤）也照实结束作业
                    log.warning(
                        "supply_process_failed_unwritten error=%s",
                        type(written).__name__,
                    )
                await self._release(grant)
            await self._finish(job, attempt, "failed", code, "failed", token=token)
            return "failed"
        await self._release(grant)
        await self._finish(
            job,
            attempt,
            "done",
            ",".join(sorted(done)) or "nothing_ready",
            "done",
            token=token,
        )
        return "done"

    async def _keep(self, entry: Entry, content: bytes, media_type: str) -> Original:
        """原件一个字节不改先存下，按摘要去重，记出处（0045 第 3.4 节）。没交付成的从取到时
        起留 30 天（同交付后的留存期，0045 第 3.4 节没另写）；交付成了从交付时重新算。"""
        sha = hashlib.sha256(content).hexdigest()
        found = await self.repo.find_original(sha)
        key = f"supply/originals/{sha[:2]}/{sha}"
        if found is not None:
            if found.purged_at is not None:
                # 以前那份留满 30 天删了内容，这次又取到同一份：内容放回去，再从头留 30 天
                await self.objects.put(found.object_key, content, media_type)
                await self.repo.revive_original(found.id, self.now() + RETENTION)
            return found
        await self.objects.put(key, content, media_type)
        return await self.repo.save_original(
            Original(
                id=self.new_id(),
                sha256=sha,
                channel_id=entry.channel_id,
                channel_doc_id=entry.channel_doc_id,
                source_url=entry.url,
                media_type=media_type,
                size_bytes=len(content),
                object_key=key,
                fetched_at=self.now(),
                purge_after=self.now() + RETENTION,
            ),
            {
                "title": entry.title,
                "announcement_id": entry.announcement_id,
                "disclosed_on": entry.disclosed_on.isoformat()
                if entry.disclosed_on
                else None,
                "revised": entry.revised,
            },
        )

    async def _settle_waiting(self, job_id: str, status: str, *, reason: str) -> None:
        waiting = await self.repo.waiting_items(job_id)
        await self.repo.settle_items(
            [item.id for item, _ in waiting], status, self.now(), reason=reason
        )


def _next_channel(
    channels: list[Channel],
    item: DemandItem,
    demand: Demand,
    tried: tuple[str, ...],
) -> Channel | None:
    """下一个能供这一项、满足这张单的供给条件、还没试过的渠道（按先后）。"""
    if item.key is None:
        return None
    conditions = Conditions.from_dict(demand.conditions)
    _, allowed = candidates(channels, item.key, conditions, skip=tried)
    return allowed[0] if allowed else None


def _beneficiaries(
    waiting: list[tuple[DemandItem, Demand]],
) -> list[dict[str, object]]:
    """为用户采集的受益人：等这一份的每一项一位（归属用户、他自己的预填、需求依据）。
    预填只用在这个人自己那份（二审第 10 条）；按描述要的，预填就是那条描述；
    带上该受益人自己的合规依据 `basis`（三审第 3 条）。"""
    out: list[dict[str, object]] = []
    for item, demand in waiting:
        prefill: dict[str, object] | None
        if item.key is not None:
            prefill = item.key.as_dict()
        else:
            prefill = dict(item.prefill) or None
        out.append(
            {
                "subject": demand.owner,
                "prefill": prefill,
                "item_ref": item.basis_ref,
                "basis": basis_of(demand, item),
            }
        )
    return out


def _default_description(entry: Entry, key: DescriptionKey | None) -> dict[str, object]:
    """渠道条目给的未确认候选描述（三审第 12 条）：显式标 `confirmed: False`，与用户预填分开。"""
    return {
        "title": entry.title,
        "doc_type": entry.doc_type or (key.doc_type if key else None),
        "subject": entry.subject or (key.subject if key else None),
        "disclosed_on": entry.disclosed_on.isoformat() if entry.disclosed_on else None,
        "edition": entry.edition,
        "topics": list(entry.topics),
        "confirmed": False,
    }


def _expected_readings(media_type: str, facts: bool) -> set[str]:
    form = form_of(media_type)
    if form == "pdf":
        return {"original", "pages", "table"} if facts else {"original", "pages"}
    if form in ("html", "text"):
        return {"original", "text"}
    return {"original"}


def _url_title(url: str, filename: str | None = None) -> str:
    """直接按 URL 取非 HTML 文件时的默认标题（三审第 16 条）：优先取 Content-Disposition
    文件名，否则取 URL 路径末段文件名（去掉查询串）。"""
    if filename:
        cleaned = filename.strip()
        if cleaned:
            return cleaned[:200]
    parts = urlsplit(url)
    segment = unquote(parts.path.rstrip("/").rsplit("/", 1)[-1]).strip()
    if segment:
        return segment[:200]
    return (parts.hostname or url)[:200]


def _linked_type(media_type: str) -> str:
    """连到的文件没有预填类型时：表格归「其他表格」，别的归「其他文本」（词表第 2 版）。"""
    return "other_table" if form_of(media_type) == "table" else "other_text"


def _file_name(entry: Entry, media_type: str) -> str:
    title = "".join(c for c in entry.title if c not in '\\/:*?"<>|')[:150]
    base = media_type.split(";", 1)[0].strip().lower()
    suffix = _SUFFIX.get(base)
    if suffix is None:
        path = urlsplit(entry.url).path.lower()
        suffix = next(
            (s for s in sorted(set(_SUFFIX.values())) if path.endswith(s)), ".pdf"
        )
    if title.lower().endswith(suffix):
        return title
    return f"{title or entry.channel_doc_id.replace(':', '-')}{suffix}"
