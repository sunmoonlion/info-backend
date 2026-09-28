"""从一个采集批次建数据集（0008-info 段二）。"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.application.ports.securities import (
    BatchItem,
    BatchReader,
    DatasetStore,
    LoadedBatch,
    ReportReader,
    ReportWordsReader,
)
from app.application.securities.dataset.basis import assign_basis
from app.application.securities.dataset.official_report import parse_key_figures
from app.application.securities.dataset.quality import (
    DEFAULT_HARD_YEARS,
    Screening,
    run_checks,
    screen,
)
from app.application.securities.dataset.sqlite_writer import (
    EXPORT_VERSION,
    build_tables,
    content_fingerprint,
    dataset_id,
    write_sqlite,
)
from app.application.securities.dataset.statement_figures import (
    figures_from_statements,
)
from app.application.securities.dataset.statements import parse_statement
from app.application.securities.report_extraction import extract_statements
from app.domain.securities import IngestionStatus, ItemKind, SecurityCode, SourceCode
from app.domain.securities.dataset import (
    BuiltDataset,
    DatasetBuildError,
    DisclosureEntry,
    OfficialFigure,
    ParsedReport,
    StatementRow,
)
from app.domain.securities.financial_catalog import (
    BASIS_RESTATED,
    BASIS_UNVERIFIED,
    STATEMENT_FIELDS,
)

logger = logging.getLogger(__name__)
_REPORT_PAGES = 25
_ANNUAL = "年报"
PUBLISHED = "published"
QUALITY_FAILED = "quality_failed"


@dataclass(frozen=True)
class DatasetSummary:
    record_id: str
    ingestion_id: str
    dataset_id: str
    data_version: str
    security_code: str
    status: str
    sha256: str
    size_bytes: int
    row_counts: dict[str, int]
    start_date: str
    end_date: str
    basis_by_year: dict[int, str]
    failed_checks: tuple[str, ...]
    report_problems: dict[str, str]


class SecurityDatasetService:
    def __init__(
        self,
        *,
        batches: BatchReader,
        reports: ReportReader,
        store: DatasetStore,
        today: Callable[[], date],
        hard_years: int = DEFAULT_HARD_YEARS,
        words: ReportWordsReader | None = None,
    ) -> None:
        if hard_years < 1:
            raise ValueError("hard_years must be at least 1")
        self._batches = batches
        self._reports = reports
        # 给了它，「主要会计数据」表认不出来的年报就改从合并报表取关键数字；不给就和以前一样
        self._words = words
        self._store = store
        self._today = today
        self._hard_years = hard_years

    async def build_latest(self, raw_code: str) -> DatasetSummary:
        code = SecurityCode(raw_code)
        ingestion_id = await self._batches.latest_succeeded(code)
        if ingestion_id is None:
            raise DatasetBuildError("no_succeeded_ingestion", code.code)
        return await self.build(ingestion_id)

    async def build(self, ingestion_id: str) -> DatasetSummary:
        batch = await self._batches.load(ingestion_id)
        if batch is None:
            raise DatasetBuildError("ingestion_not_found")
        if batch.status != IngestionStatus.SUCCEEDED.value:
            raise DatasetBuildError("ingestion_not_succeeded", batch.status)
        screening = screen(await self._statements(batch), hard_years=self._hard_years)
        rows = screening.rows
        figures, calendar, problems, explanations = await self._reports_of(batch, rows)
        decided = assign_basis(rows, figures)
        quality = run_checks(
            rows, figures, calendar, today=self._today(), screening=screening
        )
        tables = build_tables(rows, figures, calendar)
        dates = [r.report_date for group in rows.values() for r in group]
        metadata = _metadata(
            batch.code,
            rows,
            figures,
            decided,
            problems,
            explanations,
            start=min(dates),
            end=max(dates),
        )
        metadata.update(_old_data_notes(screening, quality))
        metadata.update(_statement_source_note(figures))
        identity = dataset_id(batch.code)
        fingerprint = hashlib.sha256(
            (content_fingerprint(tables) + repr(sorted(metadata.items()))).encode()
        ).hexdigest()
        version = f"{identity}-{fingerprint[:16]}"
        metadata["data_snapshot_id"] = version
        metadata["source_fingerprint"] = fingerprint
        content = await asyncio.to_thread(write_sqlite, tables, metadata)
        built = BuiltDataset(
            dataset_id=identity,
            data_version=version,
            security_code=batch.code.code,
            status=PUBLISHED if quality.passed else QUALITY_FAILED,
            content=content,
            sha256=hashlib.sha256(content).hexdigest(),
            row_counts={name: len(data) for name, (_, data) in tables.items()},
            start_date=min(dates),
            end_date=max(dates),
            quality=quality,
            metadata=metadata,
        )
        record_id = await self._store.save(built, ingestion_id=ingestion_id)
        if not quality.passed:
            logger.warning(
                "security dataset blocked code=%s checks=%s",
                batch.code,
                ",".join(quality.failed_blocking),
            )
        return DatasetSummary(
            record_id=record_id,
            ingestion_id=ingestion_id,
            dataset_id=identity,
            data_version=version,
            security_code=batch.code.code,
            status=built.status,
            sha256=built.sha256,
            size_bytes=len(content),
            row_counts=built.row_counts,
            start_date=built.start_date,
            end_date=built.end_date,
            basis_by_year={
                y: b for y, (b, _) in sorted(decided.items()) if b != BASIS_UNVERIFIED
            },
            failed_checks=quality.failed_blocking,
            report_problems=problems,
        )

    async def _statements(self, batch: LoadedBatch) -> dict[str, list[StatementRow]]:
        rows: dict[str, list[StatementRow]] = {}
        for statement in STATEMENT_FIELDS:
            items = [
                i
                for i in batch.items
                if i.kind == ItemKind.STATEMENT_DATA.value
                and i.meta.get("statement") == statement
                and i.http_status < 400
            ]
            if not items:
                raise DatasetBuildError("statement_missing", statement)
            payloads = [await self._batches.read(i) for i in items]
            rows[statement] = parse_statement(statement, payloads, batch.code)
        return rows

    async def _reports_of(
        self, batch: LoadedBatch, rows: dict[str, list[StatementRow]]
    ) -> tuple[
        list[OfficialFigure],
        list[DisclosureEntry],
        dict[str, str],
        dict[int, tuple[str, str]],
    ]:
        figures: list[OfficialFigure] = []
        calendar: list[DisclosureEntry] = []
        problems: dict[str, str] = {}
        explanations: dict[int, tuple[str, str]] = {}
        reports = sorted(
            (
                i
                for i in batch.items
                if i.kind == ItemKind.REPORT_FILE.value and i.http_status < 400
            ),
            key=lambda i: (i.meta["fiscal_year"], i.meta["official_disclosed_date"]),
        )
        if not reports:
            raise DatasetBuildError("report_missing")
        for item in reports:
            year = int(item.meta["fiscal_year"])
            title = str(item.meta["title"])
            calendar.append(
                DisclosureEntry(
                    fiscal_year=year,
                    report_type=_ANNUAL,
                    title=title,
                    official_disclosed_date=str(item.meta["official_disclosed_date"]),
                    announcement_id=str(item.meta["announcement_id"]),
                    revised=bool(item.meta.get("revised")),
                    source=SourceCode.CNINFO.value,
                    artifact_sha256=item.sha256,
                )
            )
            parsed = await self._parse(item, year, title, rows)
            if parsed.problem:
                problems[title] = parsed.problem
                continue
            figures.extend(parsed.figures)
            if parsed.explanation:
                # 说明挂在被调整的年度上；多份年报都说明同一年时，留最早披露的那份
                for restated in sorted(
                    {f.fiscal_year for f in parsed.figures if f.basis == BASIS_RESTATED}
                ):
                    explanations.setdefault(restated, (title, parsed.explanation))
        return figures, calendar, problems, explanations

    async def _parse(
        self,
        item: BatchItem,
        year: int,
        title: str,
        rows: dict[str, list[StatementRow]],
    ):
        pdf = await self._batches.read(item)
        pages = await asyncio.to_thread(
            self._reports.extract_pages, pdf, max_pages=_REPORT_PAGES
        )
        about = {
            "report_fiscal_year": year,
            "title": title,
            "disclosed_date": str(item.meta["official_disclosed_date"]),
            "revised": bool(item.meta.get("revised")),
        }
        parsed = parse_key_figures(pages, **about)
        words = self._words
        if not parsed.problem or words is None:
            return parsed
        try:
            extraction = await asyncio.to_thread(
                lambda: extract_statements(words.read_words(pdf))
            )
        except DatasetBuildError:
            return parsed
        fallback = figures_from_statements(
            extraction, reference_in_yuan=_reference(rows, year), **about
        )
        if fallback.problem:
            return ParsedReport((), None, None, f"{parsed.problem}；{fallback.problem}")
        return fallback


def _metadata(
    code: SecurityCode,
    rows: dict[str, list[StatementRow]],
    figures: list[OfficialFigure],
    decided: dict[int, tuple[str, str | None]],
    problems: dict[str, str],
    explanations: dict[int, tuple[str, str]],
    *,
    start: str,
    end: str,
) -> dict[str, str]:
    """数据集的说明。只写由数据与年报原文得出的事实；专家只引用，不自行断言。"""
    verified = sorted(y for y, (b, _) in decided.items() if b != BASIS_UNVERIFIED)
    restated = sorted(y for y, (b, _) in decided.items() if b == BASIS_RESTATED)
    reports = sorted({f.source_report for f in figures})
    meta = {
        "dataset_export_version": EXPORT_VERSION,
        "security_code": code.code,
        "market": str(code.market),
        "start_date": start,
        "end_date": end,
        "statement_source": "东方财富 F10 财务分析；第三方网站整理的数据，不是法定披露原文",
        "official_source": "巨潮资讯网年度报告原文：" + "、".join(reports),
        "verified_range": (
            f"年报 {verified[0]} 至 {verified[-1]} 年的关键科目已与年度报告原文逐项核对"
            if verified
            else "没有任何年度与年度报告原文核对成功"
        ),
        "notice_date_note": (
            "aggregator_notice_date 是第三方数据里的公告日，与法定披露日不一致；"
            "披露时点以 disclosure_calendar 为准"
        ),
        "unit_note": "金额单位为元；basic_eps 为元每股",
        "interim_note": "中报、季报行的口径为未核实",
        "license_note": "第三方整理数据的再分发许可未确认；仅供内部使用",
    }
    if restated:
        notes = []
        for year in restated:
            notes.append(
                f"{year} 年年报行为追溯调整后口径（依据《{decided[year][1]}》）；"
                f"当年原始披露值见 official_key_figures"
            )
            if decided.get(year - 1, ("", None))[0] not in ("", BASIS_RESTATED):
                notes.append(f"跨 {year - 1}/{year} 的同比与环比不可直接比较")
        meta["restatement_note"] = "；".join(notes)
    for year, (title, text) in sorted(explanations.items()):
        meta[f"restatement_explanation_{year}"] = (
            f"{year} 年数据被追溯调整的原因，《{title}》原文：{text}"
        )
    if problems:
        meta["report_parse_problems"] = "；".join(
            f"{title}：{problem}" for title, problem in sorted(problems.items())
        )
    lease = _first_year_with(rows, ("lease_liab", "useright_asset"))
    if lease == 2021:
        meta["accounting_note_lease"] = (
            "本数据集中租赁负债与使用权资产自 2021 年起出现。修订后的《企业会计准则"
            "第 21 号——租赁》对仅在境内上市的企业自 2021 年 1 月 1 日起施行：承租人"
            "确认使用权资产与租赁负债，偿还租赁负债本金和利息支付的现金计入筹资活动。"
            "因此 2021 年前后的资产负债率、经营活动现金流量不可直接比较"
        )
    return meta


def _reference(
    rows: dict[str, list[StatementRow]], year: int
) -> dict[str, dict[str, Decimal]]:
    """第三方数据里这一年的年报行，只用来核对量级。"""
    found: dict[str, dict[str, Decimal]] = {}
    for statement, group in rows.items():
        for row in group:
            if row.report_type == _ANNUAL and row.fiscal_year == year:
                found[statement] = {
                    k: Decimal(str(v)) for k, v in row.values.items() if v is not None
                }
    return found


def _statement_source_note(figures: list[OfficialFigure]) -> dict[str, str]:
    """有年报的关键数字取自合并报表时，写进数据集的说明。没有就什么都不加。"""
    reports = sorted(
        {f.source_report for f in figures if f.column_label.startswith("合并")}
    )
    if not reports:
        return {}
    return {
        "official_figures_from_statements": (
            "以下年报的「主要会计数据」表没有认出来，关键数字取自年报里的合并报表"
            "（本期列为原始披露，上期列为比较数；不含扣除非经常性损益的净利润）："
            + "、".join(reports)
        )
    }


def _old_data_notes(screening: Screening, quality) -> dict[str, str]:
    """硬性拦截范围之前的数据被剔除或不连续时，写进数据集的说明。没有这类情况就什么都不加。"""
    notes: dict[str, str] = {}
    if screening.excluded:
        listed = "；".join(
            f"{e['report_date']}（{e['report_type']}，"
            + "、".join(f"{r['rule_id']} 差 {r['residual']}" for r in e["rules"])
            + "）"
            for e in screening.excluded
        )
        notes["excluded_periods_note"] = (
            f"以下报告期的第三方数据同期勾稽不平，三张表都已剔除这一期：{listed}。"
            f"硬性拦截的范围是 {screening.first_hard_year} 年及以后；"
            "范围之前的数据有问题只剔除、不拦整个数据集"
        )
    old = [
        v
        for c in quality.checks
        if c.check_id == "Q-CONT"
        for v in c.violations
        if v.get("outside_hard_window")
    ]
    if old:
        years = "、".join(str(v["fiscal_year"]) for v in old)
        notes["old_continuity_note"] = (
            f"以下年度的期初现金与上一年的期末现金不一致：{years}。"
            f"它们在硬性拦截的范围（{screening.first_hard_year} 年及以后）之前，"
            "口径未核实，原因未知；跨这些年度的现金流量比较不可直接使用"
        )
    return notes


def _first_year_with(rows, names: tuple[str, ...]) -> int | None:
    years = [
        r.fiscal_year
        for r in rows.get("balance_sheet", [])
        if r.report_type == _ANNUAL and any(r.values.get(n) for n in names)
    ]
    return min(years) if years else None
