"""作业的执行（SDD 0045 第 3.3–3.5 节）：列 → 取 → 留存 → 交 knowbase → 加工 → 回执。

一个取数作业跨用户共享：同一渠道同一描述只取一次，等它的需求项都挂在上面。

- 尝试：每次执行记开始、结束、结果、原因。临时出错（对方 5xx、超时、knowbase 暂时
  不可用）按逐次拉长的间隔重试，有次数上限；渠道明说没有的不重试、转「无法满足」；
  内容不对的不重试、转「失败」。
- 交付可安全重试：knowbase 按渠道编号与内容摘要认同一份，引用按需求依据只记一条。
- 加工没成功原件照样交付，需求项照实写（只要原件的不因加工失败降级）。
- 原件交 knowbase 拿到回执后再留 30 天。
- 定期报告一条描述交一份；其余类型一条是一个披露时间窗，窗里列出的逐份交付
  （SDD 0049）。交给 knowbase 的描述按插件认出的细类、版本形态、事项写。
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from app.application.ports.supply import (
    Catalog,
    Knowbase,
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
from app.domain.supply.models import (
    MAX_ATTEMPTS,
    DescriptionKey,
    Entry,
    Original,
    SupplyError,
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


class JobRunner:
    def __init__(
        self,
        repo: SupplyRepository,
        catalogs: dict[str, Catalog],
        knowbase: Knowbase,
        objects: ObjectStore,
        *,
        pdf_pages: PdfPages,
        pdf_words: PdfWords,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self.repo = repo
        self.catalogs = catalogs
        self.knowbase = knowbase
        self.objects = objects
        self.pdf_pages = pdf_pages
        self.pdf_words = pdf_words
        self.now = clock
        self.new_id = new_id

    async def run(self, job_id: str) -> str:
        """跑一次；返回这次的结果（done、absent、retry、failed、skipped）。"""
        attempt = await self.repo.start_attempt(job_id, self.now())
        if attempt is None:
            return "skipped"
        job = await self.repo.get_job(job_id)
        assert job is not None
        key = DescriptionKey.from_dict(job.payload["key"])  # type: ignore[arg-type]
        try:
            outcome = await self._fetch_and_deliver(job_id, job.channel_id, key)
        except SupplyError as exc:
            retryable = exc.status >= 500 or exc.status == 429
            if retryable and job.attempts < MAX_ATTEMPTS:
                await self.repo.finish_attempt(
                    job_id,
                    attempt,
                    self.now(),
                    outcome="retry",
                    reason=exc.code,
                    job_status="waiting",
                )
                await self.repo.retry_later(
                    job_id, self.now() + _BACKOFF[min(job.attempts, len(_BACKOFF)) - 1]
                )
                return "retry"
            await self._settle_waiting(job_id, "failed", reason=exc.code)
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
                await self.repo.finish_attempt(
                    job_id,
                    attempt,
                    self.now(),
                    outcome="retry",
                    reason=reason,
                    job_status="waiting",
                )
                await self.repo.retry_later(
                    job_id, self.now() + _BACKOFF[min(job.attempts, len(_BACKOFF)) - 1]
                )
                return "retry"
            await self._settle_waiting(job_id, "failed", reason=reason)
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
            job_id, attempt, self.now(), outcome=outcome, reason=None, job_status="done"
        )
        return outcome

    async def _fetch_and_deliver(
        self, job_id: str, channel_id: str, key: DescriptionKey
    ) -> str:
        catalog = self.catalogs[channel_id]
        entries = await catalog.list(key)
        if not entries:
            # 渠道明说没有：不重试，转「无法满足」（报告还没披露、这家没有这一类、
            # 窗里没有这类公告）
            await self._settle_waiting(
                job_id, "unsatisfiable", reason="the channel has no such document"
            )
            return "absent"
        # 定期报告：新的在前，更正版是同一份资料的新版本，交最新那一条
        chosen = entries[:1] if is_periodic(key.doc_type) else entries
        waiting = await self.repo.waiting_items(job_id)
        references = [(demand.owner, item.basis_ref) for item, demand in waiting]
        delivered: list[dict[str, object]] = []
        readings: set[str] | None = None
        for entry in chosen:
            one, done = await self._deliver_one(entry, key, references)
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
                reason=None if status == "ready" else "some readings are not ready",
            )
        return "done"

    async def _deliver_one(
        self, entry: Entry, key: DescriptionKey, references: list[tuple[str, str]]
    ) -> tuple[dict[str, object], set[str]]:
        """取一份、留存、交 knowbase、加工、记引用；回（回执里的这一份，就绪的读法）。"""
        content, media_type = await self.catalogs[entry.channel_id].fetch(entry)
        original = await self._keep(entry, content, media_type)
        periodic = is_periodic(entry.doc_type)
        delivered = await self.knowbase.deliver_public(
            channel=entry.channel_id,
            channel_doc_id=entry.channel_doc_id,
            name=_file_name(entry),
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
        done = {"original"}
        if delivered.get("created"):
            done |= await self._process(material_id, version_no, content, key, facts)
        else:
            # 这一版以前交过：读法那时已加工（同一条流水线），这里不重做
            done |= {"pages"} | ({"table"} if facts else set())
        if references:
            await self.knowbase.add_references(material_id, version_no, references)
        await self.repo.mark_delivered(
            original.id, self.now(), self.now() + RETENTION, material_id, version_no
        )
        return (
            {
                "material_id": material_id,
                "version_no": version_no,
                "sha256": original.sha256,
                "readings": sorted(done),
                "channel_doc_id": entry.channel_doc_id,
                "doc_type": entry.doc_type,
            },
            done,
        )

    async def _process(
        self,
        material_id: str,
        version_no: int,
        pdf: bytes,
        key: DescriptionKey,
        facts_wanted: bool,
    ) -> set[str]:
        """凭加工授权写回读法：按页文字；定期报告全文再抽三张合并报表。没成的照实写原因。"""
        grant = await self.knowbase.processing_grant(material_id, version_no)
        done: set[str] = set()
        try:
            pages = pages_reading(pdf, self.pdf_pages)
            await self.knowbase.write_pages(grant, PAGES_VERSION, pages)
            done.add("pages")
        except SupplyError:
            raise
        except Exception as exc:
            log.warning("supply_pages_failed material=%s error=%s", material_id, exc)
            await self.knowbase.write_failed(
                grant, "pages", PAGES_VERSION, "unreadable"
            )
        if not facts_wanted:
            return done
        try:
            facts = report_statements(pdf, key, self.pdf_words)
        except Exception as exc:
            log.warning("supply_facts_failed material=%s error=%s", material_id, exc)
            await self.knowbase.write_failed(
                grant, "table", FACTS_VERSION, "unreadable"
            )
            return done
        if not facts.raw:
            reason = ";".join(facts.problems)[:450] or "no statements found"
            await self.knowbase.write_failed(grant, "table", FACTS_VERSION, reason)
            return done
        await self.knowbase.write_facts(grant, FACTS_VERSION, facts.raw, facts.standard)
        done.add("table")
        return done

    async def _keep(self, entry: Entry, content: bytes, media_type: str) -> Original:
        """原件一个字节不改先存下，按摘要去重，记出处（0045 第 3.4 节）。"""
        sha = hashlib.sha256(content).hexdigest()
        found = await self.repo.find_original(sha)
        if found is not None:
            return found
        key = f"supply/originals/{sha[:2]}/{sha}"
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


def _file_name(entry: Entry) -> str:
    title = "".join(c for c in entry.title if c not in '\\/:*?"<>|')[:150]
    return f"{title or entry.channel_doc_id}.pdf"
