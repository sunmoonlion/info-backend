"""作业的执行（SDD 0045 第 3.3–3.5 节、第四节）：列 → 取 → 留存 → 交 knowbase → 加工 → 回执；
上传资料的加工作业：领加工授权 → 读原件 → 加工 → 写回读法。

一个取数作业跨用户共享：同一渠道同一描述只取一次，等它的需求项都挂在上面。作业由调度
（`scheduler.py`）按渠道、四档、按用户轮流排出去；插件每问一次渠道都经限速。

- 尝试：每次执行记开始、结束、结果、原因。临时出错（对方 5xx、超时、knowbase 暂时
  不可用）按逐次拉长的间隔重试，有次数上限；渠道这会儿不能再问（日上限用完）放回队列、
  不算一次失败；内容不对的不重试。
- **取不到换下一个**（0045 第四节）：渠道明说没有、或这条渠道取不成（重试用完、对方拒绝），
  在等的需求项挂到来源目录里下一个能供、且满足确认时供给条件的渠道；都试过了才结束：
  渠道里都没有转「无法满足」（`channel_has_none`），有取不成的转「失败」附原因。
- 交付可安全重试：knowbase 按渠道编号与内容摘要认同一份，引用按需求依据只记一条；
  披露时间窗里以前交过的那一份不再重取，只给新来的需求记引用。
- 加工没成功原件照样交付，需求项照实写（只要原件的不因加工失败降级）。读法按内容定：
  PDF 按页文字，定期报告全文再抽三张合并报表；网页、文本、Markdown 写正文（`readings.py`）。
- 原件交 knowbase 拿到回执后再留 30 天，到期删内容（`retention.py`）。
- 渠道（0045 第 3.1 节）：渠道暂停时作业不跑、等恢复；停用或审不过的，在等的需求项转
  「无法满足」。渠道每次没取到（对方出错、取数失败，限流不算）连续失败次数加一，到
  `AUTO_PAUSE_AFTER` 次自动暂停并告警；答了（有没有都算）就清零。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

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
from app.domain.supply.catalog import AUTO_PAUSE_AFTER
from app.domain.supply.models import (
    MAX_ATTEMPTS,
    REASON_CHANNEL_DISABLED,
    REASON_CHANNEL_HAS_NONE,
    REASON_READINGS_INCOMPLETE,
    Channel,
    Demand,
    DemandItem,
    DescriptionKey,
    Entry,
    Job,
    Original,
    SupplyError,
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
# 财报抽表：四类定期报告的三张合并报表（10-28 起半年报、季报也抽，见 processing）
FACT_DOC_TYPES = tuple(REPORT_SHAPES)
# 交 knowbase 的文件名后缀按内容类型（knowbase 按后缀认类型、再核文件头）
_SUFFIX = {"pdf": ".pdf", "text": ".txt", "markdown": ".md"}

Plugins = Callable[[Channel], Catalog | None]


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
        self.now = clock
        self.new_id = new_id

    async def run(self, job_id: str) -> str:
        """跑一次；返回这次的结果（done、absent、moved、retry、failed、held、deferred、
        withdrawn、skipped）。"""
        job = await self.repo.get_job(job_id)
        if job is None or job.status not in ("queued", "waiting", "running"):
            return "skipped"
        if job.kind == "process":
            return await self._run_process(job)
        channel = await self.repo.get_channel(job.channel_id)
        if (
            channel is None
            or channel.status == "disabled"
            or channel.review != "approved"
        ):
            # 停用、审不过（撤销）的渠道：停用时已经收拾过，这里兜底
            await self._settle_waiting(
                job_id, "unsatisfiable", reason=REASON_CHANNEL_DISABLED
            )
            attempt = await self.repo.start_attempt(job_id, self.now())
            if attempt is not None:
                await self.repo.finish_attempt(
                    job_id,
                    attempt,
                    self.now(),
                    outcome="withdrawn",
                    reason=REASON_CHANNEL_DISABLED,
                    job_status="failed",
                )
            return "withdrawn"
        if channel.status == "paused":
            await self.repo.hold_job(job_id)
            return "held"
        attempt = await self.repo.start_attempt(job_id, self.now())
        if attempt is None:
            return "skipped"
        job = await self.repo.get_job(job_id)
        assert job is not None
        key = DescriptionKey.from_dict(job.payload["key"])  # type: ignore[arg-type]
        try:
            outcome = await self._fetch_and_deliver(job, channel, key)
        except ChannelDeferred as deferred:
            await self.repo.finish_attempt(
                job_id,
                attempt,
                self.now(),
                outcome="deferred",
                reason=deferred.reason,
                job_status="waiting",
            )
            await self.repo.defer(job_id, deferred.until, deferred.reason)
            return "deferred"
        except SupplyError as exc:
            retryable = exc.status >= 500 or exc.status == 429
            if retryable and job.attempts < MAX_ATTEMPTS:
                return await self._retry(job, attempt, exc.code)
            await self._give_up(job, channel, key, "failed", exc.code)
            await self.repo.finish_attempt(
                job_id,
                attempt,
                self.now(),
                outcome="failed",
                reason=exc.code,
                job_status="failed",
            )
            return "failed"
        except Exception as exc:  # 意料之外的：当临时出错重试，次数用完转失败
            log.exception("supply_job_failed job=%s", job_id)
            reason = type(exc).__name__
            if job.attempts < MAX_ATTEMPTS:
                return await self._retry(job, attempt, reason)
            await self._give_up(job, channel, key, "failed", reason)
            await self.repo.finish_attempt(
                job_id,
                attempt,
                self.now(),
                outcome="failed",
                reason=reason,
                job_status="failed",
            )
            return "failed"
        await self.repo.finish_attempt(
            job_id,
            attempt,
            self.now(),
            outcome=outcome,
            reason=None,
            job_status="done",
        )
        return outcome

    async def _retry(self, job: Job, attempt: str, reason: str) -> str:
        await self.repo.finish_attempt(
            job.id,
            attempt,
            self.now(),
            outcome="retry",
            reason=reason,
            job_status="waiting",
        )
        await self.repo.retry_later(
            job.id, self.now() + _BACKOFF[min(job.attempts, len(_BACKOFF)) - 1]
        )
        return "retry"

    # —— 换下一个渠道 ——

    async def _give_up(
        self,
        job: Job,
        channel: Channel,
        key: DescriptionKey,
        status: str,
        reason: str,
    ) -> int:
        """这条渠道取不到：在等的需求项挂到下一个能供、满足供给条件的渠道；没有下一个的
        转 `status`（附 `reason`）。回换了几项。"""
        waiting = await self.repo.waiting_items(job.id)
        if not waiting:
            return 0
        channels = await self.repo.channels()
        moves: list[tuple[str, Job, tuple[str, ...], str]] = []
        ended: dict[tuple[str, str], list[str]] = {}
        for item, demand in waiting:
            tried = tuple(dict.fromkeys((*item.tried_channels, channel.id)))
            following = _next_channel(channels, item, demand, tried)
            if following is None:
                # 前面哪条渠道取不成的，结局是「失败」；都只是没有，才是「渠道里没有」
                final = (status, reason)
                if status == "unsatisfiable" and item.reason not in (
                    None,
                    REASON_CHANNEL_HAS_NONE,
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
                        dedup_key=f"fetch:{following.id}:{key.text}",
                        payload={"key": key.as_dict()},
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

    # —— 取数作业 ——

    async def _ask_channel(self, channel: Channel, call):
        """问渠道（列、取）：没取到记一次失败（限流、等名额不算），答了清零。"""
        try:
            answer = await call
        except ChannelDeferred:
            raise
        except SupplyError as exc:
            if exc.code != "channel_rate_limited":
                await self._channel_failed(channel)
            raise
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

    async def _fetch_and_deliver(
        self, job: Job, channel: Channel, key: DescriptionKey
    ) -> str:
        catalog = self.plugins(channel)
        if catalog is None:
            raise SupplyError(503, "plugin_unavailable", "no plugin for the channel")
        entries = await self._ask_channel(channel, catalog.list(key))
        if not entries:
            # 渠道明说没有（报告还没披露、这家没有这一类、窗里没有这类公告）：不重试，
            # 换下一个渠道；都没有才转「无法满足」
            moved = await self._give_up(
                job, channel, key, "unsatisfiable", REASON_CHANNEL_HAS_NONE
            )
            return "moved" if moved else "absent"
        # 定期报告：新的在前，更正版是同一份资料的新版本，交最新那一条
        chosen = entries[:1] if is_periodic(key.doc_type) else entries
        waiting = await self.repo.waiting_items(job.id)
        references = [(demand.owner, item.basis_ref) for item, demand in waiting]
        delivered: list[dict[str, object]] = []
        readings: set[str] | None = None
        for entry in chosen:
            one, done = await self._deliver_one(
                channel, catalog, entry, key, references
            )
            delivered.append(one)
            readings = done if readings is None else readings & done
        receipt: dict[str, object] = {"materials": delivered}
        if len(delivered) == 1:
            receipt.update(delivered[0])
        by_status: dict[str, list[str]] = {}
        for item, _ in waiting:
            by_status.setdefault(
                wanted_readings(item.readings, readings or set()), []
            ).append(item.id)
        for status, ids in by_status.items():
            await self.repo.settle_items(
                ids,
                status,
                self.now(),
                receipt=receipt,
                reason=None if status == "ready" else REASON_READINGS_INCOMPLETE,
            )
        return "done"

    async def _deliver_one(
        self,
        channel: Channel,
        catalog: Catalog,
        entry: Entry,
        key: DescriptionKey,
        references: list[tuple[str, str]],
    ) -> tuple[dict[str, object], set[str]]:
        """取一份、留存、交 knowbase、加工、记引用；回（回执里的这一份，就绪的读法）。"""
        periodic = is_periodic(entry.doc_type)
        if not periodic:
            # 窗里的每条公告、问答是一份不变的资料（更正另是一条）：交过的不再重取
            earlier = await self.repo.delivered_original(
                entry.channel_id, entry.channel_doc_id
            )
            if earlier is not None:
                original, ready = earlier
                if references:
                    await self.knowbase.add_references(
                        str(original.knowbase_material_id),
                        int(original.knowbase_version_no or 1),
                        references,
                    )
                return self._receipt(entry, original, set(ready) | {"original"})
        content, media_type = await self._ask_channel(channel, catalog.fetch(entry))
        original = await self._keep(entry, content, media_type)
        delivered = await self.knowbase.deliver_public(
            channel=entry.channel_id,
            channel_doc_id=entry.channel_doc_id,
            name=_file_name(entry, media_type),
            content=content,
            subject=key.subject,
            doc_type=entry.doc_type,
            period_start=key.period_start if periodic else None,
            period_end=key.period_end if periodic else None,
            edition=entry.edition,
            topics=entry.topics,
            disclosed_on=entry.disclosed_on.isoformat() if entry.disclosed_on else None,
            source_url=entry.url,
        )
        material_id = str(delivered["material_id"])
        version_no = int(delivered["version_no"])
        facts = entry.doc_type in FACT_DOC_TYPES and entry.edition == "full"
        form = form_of(media_type)
        done = {"original"}
        if delivered.get("created"):
            grant = await self.knowbase.processing_grant(material_id, version_no)
            done |= await self._process(
                grant,
                material_id,
                content,
                media_type,
                key if facts else None,
                title=entry.title,
                published_on=entry.disclosed_on,
            )
        else:
            # 这一版以前交过：读法那时已加工（同一条流水线），这里不重做
            done |= _readings_of(form, facts)
        if references:
            await self.knowbase.add_references(material_id, version_no, references)
        await self.repo.mark_delivered(
            original.id,
            self.now(),
            self.now() + RETENTION,
            material_id,
            version_no,
            sorted(done),
        )
        kept = Original(
            **{
                **original.__dict__,
                "knowbase_material_id": material_id,
                "knowbase_version_no": version_no,
            }
        )
        return self._receipt(entry, kept, done)

    @staticmethod
    def _receipt(
        entry: Entry, original: Original, done: set[str]
    ) -> tuple[dict[str, object], set[str]]:
        return (
            {
                "material_id": str(original.knowbase_material_id),
                "version_no": int(original.knowbase_version_no or 1),
                "sha256": original.sha256,
                "readings": sorted(done),
                "channel_doc_id": entry.channel_doc_id,
                "doc_type": entry.doc_type,
            },
            done,
        )

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
                reading = text_reading(
                    content, media_type, title=title, published_on=published_on
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
            pages = pages_reading(pdf, self.pdf_pages)
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
            facts = report_statements(pdf, key, self.pdf_words)
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

    async def _run_process(self, job: Job) -> str:
        """上传资料的加工（0045 第 3.4 节）：凭本人的加工依据领一条只这一版、只这个目的
        的加工授权，经 knowbase 接口读原件（不碰桶、不留存），加工，写回读法，交还授权。
        资料在加工中被撤销、清除的，knowbase 拒绝写回，作业照实结束。"""
        attempt = await self.repo.start_attempt(job.id, self.now())
        if attempt is None:
            return "skipped"
        job = await self.repo.get_job(job.id) or job
        owner = str(job.payload["owner"])
        material_id = str(job.payload["material_id"])
        version_no = int(job.payload["version_no"])  # type: ignore[arg-type]
        try:
            if self.reader is None:
                raise SupplyError(
                    503, "reader_not_configured", "user-data-read is not set"
                )
            grant = await self.knowbase.processing_grant(
                material_id, version_no, owner=owner
            )
            try:
                content, media_type = await self.reader.content(
                    grant, material_id, version_no
                )
                done = await self._process(
                    grant, material_id, content, media_type, None, recognize=True
                )
            finally:
                try:
                    await self.reader.release(grant)
                except Exception as exc:  # 交还失败不影响结果：授权 3 天后自己过期
                    log.warning("supply_grant_release_failed error=%s", exc)
        except SupplyError as exc:
            if (exc.status >= 500 or exc.status == 429) and job.attempts < MAX_ATTEMPTS:
                return await self._retry(job, attempt, exc.code)
            await self.repo.finish_attempt(
                job.id,
                attempt,
                self.now(),
                outcome="failed",
                reason=exc.code,
                job_status="failed",
            )
            return "failed"
        except Exception as exc:
            log.exception("supply_process_failed job=%s", job.id)
            if job.attempts < MAX_ATTEMPTS:
                return await self._retry(job, attempt, type(exc).__name__)
            await self.repo.finish_attempt(
                job.id,
                attempt,
                self.now(),
                outcome="failed",
                reason=type(exc).__name__,
                job_status="failed",
            )
            return "failed"
        await self.repo.finish_attempt(
            job.id,
            attempt,
            self.now(),
            outcome="done",
            reason=",".join(sorted(done)) or "nothing_ready",
            job_status="done",
        )
        return "done"

    async def _keep(self, entry: Entry, content: bytes, media_type: str) -> Original:
        """原件一个字节不改先存下，按摘要去重，记出处（0045 第 3.4 节）。"""
        sha = hashlib.sha256(content).hexdigest()
        found = await self.repo.find_original(sha)
        key = f"supply/originals/{sha[:2]}/{sha}"
        if found is not None:
            if found.purged_at is not None:
                # 以前那份留满 30 天删了内容，这次又取到同一份：内容放回去，再从头留 30 天
                await self.objects.put(found.object_key, content, media_type)
                await self.repo.revive_original(found.id)
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
    conditions = Conditions.from_dict(demand.conditions)
    _, allowed = candidates(channels, item.key, conditions, skip=tried)
    return allowed[0] if allowed else None


def _readings_of(form: str | None, facts: bool) -> set[str]:
    if form == "pdf":
        return {"pages"} | ({"table"} if facts else set())
    if form in ("html", "text"):
        return {"text"}
    return set()


def _file_name(entry: Entry, media_type: str) -> str:
    title = "".join(c for c in entry.title if c not in '\\/:*?"<>|')[:150]
    base = media_type.split(";", 1)[0].strip().lower()
    suffix = (
        _SUFFIX["markdown"]
        if base == "text/markdown"
        else _SUFFIX["text"]
        if base == "text/plain"
        else _SUFFIX["pdf"]
    )
    return f"{title or entry.channel_doc_id}{suffix}"
