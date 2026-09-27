"""建数据集（0008-info 段二）：解析、口径判定、质量检查、建库。

夹具来自 2026-09-27 实采的 600009：报表只裁了行与列，年报只留「主要会计数据」页的
抽取结果，数值都没有改。不访问外网，不需要数据库。
"""

from __future__ import annotations

import copy
import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from app.application.ports.securities import BatchItem, LoadedBatch
from app.application.securities.dataset.basis import assign_basis
from app.application.securities.dataset.official_report import parse_key_figures
from app.application.securities.dataset.quality import run_checks
from app.application.securities.dataset.statements import parse_statement
from app.application.securities.dataset_service import (
    PUBLISHED,
    QUALITY_FAILED,
    SecurityDatasetService,
)
from app.domain.securities import SecurityCode
from app.domain.securities.dataset import (
    BuiltDataset,
    DatasetBuildError,
    DisclosureEntry,
    ReportPage,
)
from app.domain.securities.financial_catalog import (
    METRICS,
    RECONCILIATION_RULES,
    STATEMENT_FIELDS,
)
from app.infrastructure.securities import PdfPlumberReportReader

FIXTURES = Path(__file__).parent / "fixtures" / "securities" / "dataset"
CODE = SecurityCode("600009")
TODAY = date(2026, 9, 27)
STATEMENTS = tuple(STATEMENT_FIELDS)


def payloads(statement: str) -> list[bytes]:
    return [(FIXTURES / f"{statement}_{n}.json").read_bytes() for n in (1, 2)]


def reports() -> list[dict]:
    return json.loads((FIXTURES / "annual_reports.json").read_text())


def pages_of(report: dict) -> list[ReportPage]:
    return [
        ReportPage(
            p["page_number"],
            tuple(tuple(tuple(r) for r in t) for t in p["tables"]),
            p["text"],
        )
        for p in report["pages"]
    ]


def parse(report: dict, pages: list[ReportPage] | None = None):
    meta = report["meta"]
    return parse_key_figures(
        pages if pages is not None else pages_of(report),
        report_fiscal_year=meta["fiscal_year"],
        title=meta["title"],
        disclosed_date=meta["official_disclosed_date"],
        revised=meta["revised"],
    )


def all_rows():
    return {s: parse_statement(s, payloads(s), CODE) for s in STATEMENTS}


def all_figures(only: set[int] | None = None):
    found = []
    for report in reports():
        if only is None or report["meta"]["fiscal_year"] in only:
            found.extend(parse(report).figures)
    return found


def calendar():
    return [
        DisclosureEntry(
            fiscal_year=r["meta"]["fiscal_year"],
            report_type="年报",
            title=r["meta"]["title"],
            official_disclosed_date=r["meta"]["official_disclosed_date"],
            announcement_id=r["meta"]["announcement_id"],
            revised=r["meta"]["revised"],
            source="cninfo",
            artifact_sha256=r["sha256"],
        )
        for r in reports()
    ]


def annual(rows, statement: str, year: int):
    return next(
        r for r in rows[statement] if r.report_type == "年报" and r.fiscal_year == year
    )


# ---------------------------------------------------------------- 报表解析


def test_statement_rows_keep_the_catalog_fields_and_the_period():
    rows = parse_statement("income_statement", payloads("income_statement"), CODE)
    assert [r.report_date for r in rows] == sorted(r.report_date for r in rows)
    assert len(rows) == 14
    row = next(r for r in rows if r.report_date == "2025-12-31")
    assert (row.report_type, row.fiscal_year, row.currency) == ("年报", 2025, "CNY")
    assert row.values["operate_income"] == 13346192164.12
    assert row.values["parent_netprofit"] == 2116775136.25
    assert set(row.values) == {f[0] for f in STATEMENT_FIELDS["income_statement"]}
    assert (row.basis, row.verified, row.verified_against) == ("未核实", False, None)
    assert row.aggregator_notice_date == "2026-04-30"


def edited(statement: str, change) -> list[bytes]:
    body = json.loads(payloads(statement)[0])
    change(body["data"])
    return [json.dumps(body).encode(), payloads(statement)[1]]


@pytest.mark.parametrize(
    ("change", "code"),
    [
        (
            lambda d: d[0].update(SECURITY_CODE="600519"),
            "statement_of_another_security",
        ),
        (lambda d: d[0].update(REPORT_TYPE="预告"), "statement_row_invalid"),
        (lambda d: d[0].update(REPORT_DATE="soon"), "statement_row_invalid"),
        (lambda d: d[0].update(TOTAL_ASSETS="a lot"), "statement_value_invalid"),
        (lambda d: d[0].update(TOTAL_ASSETS=True), "statement_value_invalid"),
        (lambda d: d[0].update(TOTAL_ASSETS=float("inf")), "statement_value_invalid"),
        (lambda d: d.append("not a row"), "statement_unreadable"),
    ],
)
def test_statement_rows_that_cannot_be_trusted_are_refused(change, code):
    with pytest.raises(DatasetBuildError) as caught:
        parse_statement("balance_sheet", edited("balance_sheet", change), CODE)
    assert caught.value.code == code


def test_the_same_period_with_two_different_values_is_a_conflict():
    body = json.loads(payloads("balance_sheet")[0])
    twin = copy.deepcopy(body["data"][0])
    twin["TOTAL_ASSETS"] += 1
    again = json.dumps({"data": [twin]}).encode()
    with pytest.raises(DatasetBuildError, match="statement_conflict"):
        parse_statement("balance_sheet", [*payloads("balance_sheet"), again], CODE)
    same = json.dumps({"data": [body["data"][0]]}).encode()
    rows = parse_statement("balance_sheet", [*payloads("balance_sheet"), same], CODE)
    assert len(rows) == 14  # 内容相同的重复不算冲突


@pytest.mark.parametrize(
    ("payload", "code"),
    [
        (b"<html>blocked</html>", "statement_unreadable"),
        (b'{"message": "none"}', "statement_unreadable"),
        (b'{"data": []}', "statement_empty"),
    ],
)
def test_unreadable_statement_payloads(payload, code):
    with pytest.raises(DatasetBuildError) as caught:
        parse_statement("cash_flow", [payload], CODE)
    assert caught.value.code == code


# ---------------------------------------------------------------- 年报解析


def test_every_collected_report_is_parsed():
    parsed = [parse(r) for r in reports()]
    assert [p.problem for p in parsed] == [None] * 9
    assert [p.page for p in parsed] == [6, 6, 6, 6, 6, 7, 6, 6, 6]
    assert sum(len(p.figures) for p in parsed) == 177


def test_the_2025_report_gives_three_years_with_page_references():
    figures = parse(reports()[-1]).figures
    own = {f.item: f for f in figures if f.fiscal_year == 2025}
    assert own["operate_income"].value == 13346192164.12
    assert own["total_profit"].value == 3019986074.44
    assert own["parent_netprofit"].value == 2116775136.25
    assert own["deduct_parent_netprofit"].value == 2054295543.14
    assert own["netcash_operate"].value == 5981731896.44
    assert own["total_parent_equity"].value == 42539846961.27
    assert own["total_assets"].value == 71678547661.74
    assert {f.basis for f in own.values()} == {"原始披露"}
    assert {(f.page, f.disclosed_date) for f in figures} == {(6, "2026-04-30")}
    assert {f.basis for f in figures if f.fiscal_year < 2025} == {"比较数"}
    assert sorted({f.fiscal_year for f in figures}) == [2023, 2024, 2025]


def test_the_2022_report_separates_restated_from_originally_reported():
    report = next(r for r in reports() if r["meta"]["fiscal_year"] == 2022)
    parsed = parse(report)
    of_2021 = {(f.item, f.basis): f for f in parsed.figures if f.fiscal_year == 2021}
    assert of_2021[("operate_income", "追溯调整后")].value == 8154776878.02
    assert of_2021[("operate_income", "原始披露")].value == 3727797262.22
    assert of_2021[("total_assets", "追溯调整后")].value == 67842539413.31
    assert of_2021[("total_assets", "原始披露")].value == 51426088634.23
    assert of_2021[("operate_income", "追溯调整后")].column_label == "2021年 调整后"
    assert of_2021[("operate_income", "原始披露")].column_label == "2021年 调整前"
    # 增减百分比那一列后面的 2020 年不能被当成 2021 年的延续
    of_2020 = {f.item: f for f in parsed.figures if f.fiscal_year == 2020}
    assert of_2020["operate_income"].value == 4303465087.94
    assert of_2020["operate_income"].basis == "比较数"
    assert "同一控制下企业合并" in (parsed.explanation or "")
    assert "不适用" not in [f.item for f in parsed.figures]


def test_reports_without_a_restatement_carry_no_explanation():
    assert parse(reports()[-1]).explanation is None


def test_percent_change_and_not_applicable_cells_are_never_read_as_amounts():
    for report in reports():
        for figure in parse(report).figures:
            assert abs(figure.value) > 1_000_000, figure


def rewritten(report: dict, change) -> list[ReportPage]:
    clone = copy.deepcopy(report)
    change(clone["pages"])
    return pages_of(clone)


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        (lambda p: p.clear(), "table_not_found"),
        (lambda p: [x.update(tables=[]) for x in p], "table_not_found"),
        (
            lambda p: p[0].update(text=p[0]["text"].replace("单位：元", "")),
            "unit_not_stated",
        ),
        (
            lambda p: p[0]["tables"][0].__setitem__(
                slice(1, None), [r for r in p[0]["tables"][0][1:] if r[0] == ""]
            ),
            "layout_not_recognized",
        ),
        (
            lambda p: p[0]["tables"][0].__setitem__(
                0, ["主要会计数据", "本期", "上期", "增减", "上上期"]
            ),
            "layout_not_recognized",
        ),
    ],
)
def test_a_layout_that_is_not_recognized_is_reported_not_guessed(change, problem):
    parsed = parse(reports()[-1], rewritten(reports()[-1], change))
    assert parsed.problem == problem and parsed.figures == ()


@pytest.mark.parametrize(
    ("unit", "factor"), [("千元", 1e3), ("万元", 1e4), ("百万元", 1e6)]
)
def test_amounts_are_converted_to_yuan(unit, factor):
    pages = rewritten(
        reports()[-1],
        lambda p: p[0].update(text=p[0]["text"].replace("单位：元", f"单位：{unit}")),
    )
    own = {
        f.item: f.value
        for f in parse(reports()[-1], pages).figures
        if f.fiscal_year == 2025
    }
    assert own["operate_income"] == round(13346192164.12 * factor, 2)


def test_pdf_reader_extracts_tables_only_from_the_key_page():
    pdf = (FIXTURES / "annual_2025_page6.pdf").read_bytes()
    pages = PdfPlumberReportReader().extract_pages(pdf, max_pages=5)
    assert len(pages) == 1 and pages[0].page_number == 1
    assert pages[0].tables[0][0][0] == "主要会计数据"
    parsed = parse_key_figures(
        pages,
        report_fiscal_year=2025,
        title="上海机场2025年年度报告",
        disclosed_date="2026-04-30",
        revised=False,
    )
    assert parsed.problem is None and len(parsed.figures) == 21


@pytest.mark.parametrize("junk", [b"", b"not a pdf", b"%PDF-1.4 broken", b"PK\x03\x04"])
def test_pdf_reader_refuses_what_it_cannot_read(junk):
    with pytest.raises(DatasetBuildError, match="report_unreadable"):
        PdfPlumberReportReader().extract_pages(junk, max_pages=5)


# ---------------------------------------------------------------- 口径判定


def test_basis_matches_what_was_verified_by_hand():
    """MVP-03：和 2026-09-27 探针里手工核对的结果一致。"""
    rows = all_rows()
    decided = assign_basis(rows, all_figures())
    assert {y: b for y, (b, _) in decided.items() if y >= 2017} == {
        2017: "原始披露",
        2018: "原始披露",
        2019: "原始披露",
        2020: "原始披露",
        2021: "追溯调整后",
        2022: "原始披露",
        2023: "原始披露",
        2024: "原始披露",
        2025: "原始披露",
    }
    assert decided[2021][1] == "上海机场2022年年度报告"
    assert decided[2025][1] == "上海机场2025年年度报告"
    assert decided[2019][1] == "2019年年度报告"
    for statement in STATEMENTS:
        row = annual(rows, statement, 2021)
        assert (row.basis, row.verified) == ("追溯调整后", True)
        assert row.verified_against == "上海机场2022年年度报告"


def test_years_without_any_report_stay_unverified():
    rows = all_rows()
    decided = assign_basis(rows, all_figures({2020, 2021, 2022}))
    # 2015、2016 年没有任何年报提到；2018 年只在 2020 年年报里作为比较数出现
    assert decided[2015] == ("未核实", None) and decided[2016] == ("未核实", None)
    assert decided[2018] == ("未核实", None)
    assert decided[2023] == ("未核实", None)
    assert decided[2020][0] == "原始披露" and decided[2021][0] == "追溯调整后"


def test_the_later_report_vouches_for_the_original_when_the_own_report_is_absent():
    rows = all_rows()
    body = json.loads(payloads("income_statement")[0])
    # 把报表里的 2021 年换成当年原始披露的数；2021 年年报没采到，只有 2022 年年报
    originals = {
        f.item: f.value
        for f in all_figures({2022})
        if f.fiscal_year == 2021 and f.basis == "原始披露"
    }
    for statement, item, name in (
        ("income_statement", "operate_income", "operate_income"),
        ("income_statement", "parent_netprofit", "parent_netprofit"),
        ("income_statement", "deduct_parent_netprofit", "deduct_parent_netprofit"),
        ("cash_flow", "netcash_operate", "netcash_operate"),
        ("balance_sheet", "total_parent_equity", "total_parent_equity"),
        ("balance_sheet", "total_assets", "total_assets"),
    ):
        annual(rows, statement, 2021).values[name] = originals[item]
    decided = assign_basis(rows, all_figures({2022}))
    assert decided[2021] == ("原始披露", "上海机场2022年年度报告")
    assert body  # 夹具本身没有被改动


def test_a_silent_restatement_is_still_a_restatement():
    """后面的年报没标「调整后」，但比较数与当年披露不一致、与报表一致。"""
    rows = all_rows()
    figures = all_figures({2023, 2024})
    moved = [
        f
        if not (f.fiscal_year == 2023 and f.report_fiscal_year == 2023)
        else type(f)(**{**f.__dict__, "value": f.value + 1000.0})
        for f in figures
    ]
    decided = assign_basis(rows, moved)
    assert decided[2023] == ("追溯调整后", "上海机场2024年年度报告")


def test_a_number_that_matches_no_report_is_unverified():
    rows = all_rows()
    annual(rows, "income_statement", 2024).values["operate_income"] += 100.0
    decided = assign_basis(rows, all_figures())
    assert decided[2024] == ("未核实", None)
    assert all(annual(rows, s, 2024).basis == "未核实" for s in STATEMENTS)
    assert decided[2025][0] == "原始披露"


def test_a_difference_within_one_yuan_is_tolerated():
    rows = all_rows()
    annual(rows, "balance_sheet", 2025).values["total_assets"] += 0.9
    assert assign_basis(rows, all_figures())[2025][0] == "原始披露"


def test_interim_rows_are_never_given_a_basis():
    rows = all_rows()
    assign_basis(rows, all_figures())
    interim = [r for group in rows.values() for r in group if r.report_type != "年报"]
    assert len(interim) == 9
    assert {(r.basis, r.verified, r.verified_against) for r in interim} == {
        ("未核实", False, None)
    }


# ---------------------------------------------------------------- 质量检查


def checked(rows=None, figures=None, entries=None, today=TODAY):
    rows = rows if rows is not None else all_rows()
    figures = figures if figures is not None else all_figures()
    assign_basis(rows, figures)
    report = run_checks(
        rows, figures, entries if entries is not None else calendar(), today=today
    )
    return report, {c.check_id: c for c in report.checks}


def test_real_data_passes_every_check():
    """MVP-04：九条同期勾稽全部平衡；跨期只有 2021 年不连续且有解释。"""
    report, by_id = checked()
    assert report.passed and report.failed_blocking == ()
    assert [c.check_id for c in report.checks] == [
        "Q-STRUCT",
        *[f"Q-{r.rule_id}" for r in RECONCILIATION_RULES],
        "Q-CONT",
        "Q-OFFICIAL",
        "Q-BASIS",
        "Q-DISCLOSURE",
        "Q-FRESH",
    ]
    for rule in RECONCILIATION_RULES:
        check = by_id[f"Q-{rule.rule_id}"]
        assert check.passed and check.checked >= 13 and check.violations == ()
    continuity = by_id["Q-CONT"]
    assert continuity.passed and continuity.checked == 10
    assert [v["fiscal_year"] for v in continuity.violations] == [2021]
    assert continuity.violations[0]["diff"] == 53538572.8
    assert "2021" in continuity.note
    assert by_id["Q-OFFICIAL"].checked == 55
    assert by_id["Q-BASIS"].checked == 27 and "27/27" in by_id["Q-BASIS"].note


@pytest.mark.parametrize(
    ("statement", "year", "name", "failing"),
    [
        ("balance_sheet", 2016, "total_liabilities", {"Q-R01", "Q-R05"}),
        ("balance_sheet", 2016, "total_current_assets", {"Q-R04"}),
        ("income_statement", 2016, "income_tax", {"Q-R07"}),
        ("income_statement", 2016, "minority_interest", {"Q-R06"}),
        ("cash_flow", 2015, "netcash_invest", {"Q-R08"}),
    ],
)
def test_a_damaged_number_is_caught_by_reconciliation(statement, year, name, failing):
    """MVP-05：人为改坏一个数字，质量检查拦住。"""
    rows = all_rows()
    annual(rows, statement, year).values[name] += 12345.0
    report, _ = checked(rows)
    assert not report.passed
    assert set(report.failed_blocking) == failing


def test_a_key_figure_that_contradicts_the_report_blocks():
    """营业收入不参与勾稽；它靠与年报原文的比对发现。对不上是来源间矛盾，要拦。"""
    rows = all_rows()
    annual(rows, "income_statement", 2025).values["operate_income"] += 5000.0
    report, by_id = checked(rows)
    assert annual(rows, "income_statement", 2025).basis == "未核实"
    assert report.failed_blocking == ("Q-BASIS",)
    assert {v["fiscal_year"] for v in by_id["Q-BASIS"].violations} == {2025}
    assert "24/27" in by_id["Q-BASIS"].note


def test_an_unexplained_break_in_cash_continuity_blocks():
    rows = all_rows()
    target = annual(rows, "cash_flow", 2024)
    target.values["begin_cce"] += 1_000_000.0
    target.values["end_cce"] += 1_000_000.0  # 保持同期勾稽平衡，只破坏跨期
    report, by_id = checked(rows)
    assert report.failed_blocking == ("Q-CONT",)
    years = [v["fiscal_year"] for v in by_id["Q-CONT"].violations]
    assert years == [2024, 2025, 2021]  # 没解释的排在前面


def test_a_figure_that_disagrees_with_its_own_report_blocks():
    rows = all_rows()
    figures = all_figures()
    assign_basis(rows, figures)
    annual(rows, "balance_sheet", 2023).values["total_assets"] += 777.0
    # 口径已经标好之后数据被改动：勾稽与原文比对都要发现
    report = run_checks(rows, figures, calendar(), today=TODAY)
    assert set(report.failed_blocking) == {"Q-R01", "Q-R02", "Q-R04", "Q-OFFICIAL"}
    official = next(c for c in report.checks if c.check_id == "Q-OFFICIAL")
    assert official.violations[0]["item"] == "total_assets"
    assert official.violations[0]["page"] == 6


def test_missing_statement_or_required_field_blocks():
    rows = all_rows()
    rows["cash_flow"] = []
    report, _ = checked(rows)
    assert "Q-STRUCT" in report.failed_blocking
    rows = all_rows()
    annual(rows, "balance_sheet", 2025).values["total_assets"] = None
    report, by_id = checked(rows)
    assert "Q-STRUCT" in report.failed_blocking
    assert by_id["Q-STRUCT"].violations[0]["field"] == "total_assets"


def test_no_report_at_all_blocks_publication():
    report, by_id = checked(figures=[], entries=[])
    assert "Q-BASIS" in report.failed_blocking
    assert by_id["Q-BASIS"].checked == 0


def test_warnings_do_not_block():
    report, by_id = checked(entries=calendar()[:-1], today=date(2027, 6, 1))
    assert report.passed
    assert not by_id["Q-DISCLOSURE"].passed and not by_id["Q-FRESH"].passed
    assert by_id["Q-DISCLOSURE"].violations == ({"fiscal_year": 2025},)
    assert by_id["Q-FRESH"].violations[0]["latest"] == "2026-06-30"


# ---------------------------------------------------------------- 建库


class FakeBatches:
    def __init__(self, status: str = "succeeded") -> None:
        self.status = status
        self.contents: dict[int, bytes] = {}
        self.items: list[BatchItem] = []
        for statement in STATEMENTS:
            for body in payloads(statement):
                self._add(
                    "statement_data", "eastmoney-f10", body, {"statement": statement}
                )
        self.pages: dict[bytes, list[ReportPage]] = {}
        for report in reports():
            pdf = b"%PDF-" + report["sha256"].encode()
            self.pages[pdf] = pages_of(report)
            self._add("report_file", "cninfo", pdf, report["meta"], report["sha256"])

    def _add(self, kind, source, content, meta, sha256=None):
        seq = len(self.items) + 1
        self.contents[seq] = content
        self.items.append(
            BatchItem(seq, source, kind, sha256 or f"{seq:064x}", 200, meta, seq)
        )

    async def load(self, ingestion_id: str):
        if ingestion_id != "batch-1":
            return None
        return LoadedBatch("batch-1", CODE, self.status, tuple(self.items))

    async def latest_succeeded(self, code):
        return "batch-1" if code == CODE and self.status == "succeeded" else None

    async def read(self, item: BatchItem) -> bytes:
        return self.contents[item.locator]

    def extract_pages(self, pdf: bytes, *, max_pages: int):
        return self.pages[pdf]


class FakeDatasets:
    def __init__(self) -> None:
        self.saved: list[tuple[BuiltDataset, str]] = []

    async def save(self, dataset: BuiltDataset, *, ingestion_id: str) -> str:
        self.saved.append((dataset, ingestion_id))
        return f"dataset-{len(self.saved)}"


def build_service(batches: FakeBatches, store: FakeDatasets):
    return SecurityDatasetService(
        batches=batches, reports=batches, store=store, today=lambda: TODAY
    )


def opened(content: bytes, tmp_path: Path) -> sqlite3.Connection:
    path = tmp_path / "dataset.sqlite"
    path.write_bytes(content)
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


async def test_build_publishes_a_dataset_the_knowledge_service_can_read(tmp_path):
    batches, store = FakeBatches(), FakeDatasets()
    summary = await build_service(batches, store).build_latest("600009")
    assert summary.status == PUBLISHED and summary.failed_checks == ()
    assert summary.dataset_id == "sh600009-financials"
    assert summary.data_version.startswith("sh600009-financials-")
    assert len(summary.data_version.rsplit("-", 1)[1]) == 16
    assert summary.basis_by_year[2021] == "追溯调整后"
    assert summary.report_problems == {}
    assert (summary.start_date, summary.end_date) == ("2015-12-31", "2026-06-30")
    built, ingestion_id = store.saved[0]
    assert ingestion_id == "batch-1" and summary.record_id == "dataset-1"
    db = opened(built.content, tmp_path)
    tables = {r[0] for r in db.execute("select name from sqlite_master")}
    assert tables == {
        "balance_sheet",
        "income_statement",
        "cash_flow",
        "official_key_figures",
        "disclosure_calendar",
        "field_dictionary",
        "metric_dictionary",
        "reconciliation_rules",
        "dataset_metadata",
    }
    meta = dict(db.execute("select key, value from dataset_metadata"))
    # 知识服务按这三个键取版本与期间
    assert meta["data_snapshot_id"] == summary.data_version
    assert (meta["start_date"], meta["end_date"]) == ("2015-12-31", "2026-06-30")
    assert "2021 年年报行为追溯调整后口径" in meta["restatement_note"]
    assert "跨 2020/2021 的同比与环比不可直接比较" in meta["restatement_note"]
    assert "同一控制下企业合并" in meta["restatement_explanation_2021"]
    assert "《上海机场2022年年度报告》原文" in meta["restatement_explanation_2021"]
    assert "restatement_explanation_2022" not in meta
    assert "2021 年 1 月 1 日起施行" in meta["accounting_note_lease"]
    assert "仅供内部使用" in meta["license_note"]
    assert db.execute(
        "select basis, verified, verified_against, operate_income from income_statement"
        " where report_type='年报' and fiscal_year=2021"
    ).fetchone() == ("追溯调整后", 1, "上海机场2022年年度报告", 8154776878.02)
    assert db.execute(
        "select official_disclosed_date from disclosure_calendar where fiscal_year=2021"
    ).fetchone() == ("2022-04-16",)
    assert db.execute("select count(*) from metric_dictionary").fetchone() == (
        len(METRICS),
    )
    assert summary.row_counts["official_key_figures"] == 177


async def test_rules_and_metric_hints_run_as_written_on_the_dataset(tmp_path):
    batches, store = FakeBatches(), FakeDatasets()
    await build_service(batches, store).build("batch-1")
    db = opened(store.saved[0][0].content, tmp_path)
    for _, _, table, expression in db.execute(
        "select * from reconciliation_rules"
    ).fetchall():
        worst = db.execute(
            f"select max(abs({expression})) from {table} "  # noqa: S608
            f"where ({expression}) is not null"
        ).fetchone()[0]
        assert worst is not None and worst <= 1.0
    for name, _, table, hint, *_ in db.execute(
        "select * from metric_dictionary"
    ).fetchall():
        if "+" in table or "期初" in hint:
            continue  # 跨表的口径只给公式说明，由使用方自己关联
        value = db.execute(
            f"select {hint} from {table} "  # noqa: S608
            "where report_type='年报' and fiscal_year=2025"
        ).fetchone()[0]
        assert isinstance(value, float), name


async def test_the_same_input_gives_the_same_version_and_file():
    first, second = FakeDatasets(), FakeDatasets()
    a = await build_service(FakeBatches(), first).build("batch-1")
    b = await build_service(FakeBatches(), second).build("batch-1")
    assert (a.data_version, a.sha256) == (b.data_version, b.sha256)
    assert first.saved[0][0].content == second.saved[0][0].content


async def test_a_changed_number_changes_the_version():
    base = await build_service(FakeBatches(), FakeDatasets()).build("batch-1")
    batches = FakeBatches()
    body = json.loads(batches.contents[1])
    body["data"][-1]["ACCOUNTS_RECE"] += 1.0  # 不参与勾稽与核对的科目
    batches.contents[1] = json.dumps(body).encode()
    changed = await build_service(batches, FakeDatasets()).build("batch-1")
    assert changed.status == PUBLISHED
    assert changed.data_version != base.data_version


async def test_a_damaged_batch_is_saved_as_failed_and_not_published():
    """MVP-05：质量检查没过的数据集不发布，但留下质量报告供查。"""
    batches, store = FakeBatches(), FakeDatasets()
    body = json.loads(batches.contents[1])
    body["data"][0]["TOTAL_LIABILITIES"] += 5000.0
    batches.contents[1] = json.dumps(body).encode()
    summary = await build_service(batches, store).build("batch-1")
    assert summary.status == QUALITY_FAILED
    assert set(summary.failed_checks) == {"Q-R01", "Q-R05"}
    built = store.saved[0][0]
    assert built.status == QUALITY_FAILED and not built.quality.passed
    report = built.quality.as_dict()
    failed = next(c for c in report["checks"] if c["check_id"] == "Q-R01")
    assert failed["violations"] == [
        {"report_date": "2026-06-30", "report_type": "中报", "residual": -5000.0}
    ]


async def test_an_unreadable_report_is_recorded_and_its_year_is_unverified(tmp_path):
    batches, store = FakeBatches(), FakeDatasets()
    pdf = next(k for k, v in batches.pages.items() if v[0].text.count("2025年") > 3)
    batches.pages[pdf] = [ReportPage(1, (), "封面")]
    summary = await build_service(batches, store).build("batch-1")
    assert summary.report_problems == {"上海机场2025年年度报告": "table_not_found"}
    assert 2025 not in summary.basis_by_year and summary.status == PUBLISHED
    meta = dict(
        opened(store.saved[0][0].content, tmp_path).execute(
            "select key, value from dataset_metadata"
        )
    )
    assert meta["report_parse_problems"] == "上海机场2025年年度报告：table_not_found"
    assert "2017 至 2024 年" in meta["verified_range"]


@pytest.mark.parametrize(
    ("prepare", "code"),
    [
        (lambda b: setattr(b, "status", "failed"), "ingestion_not_succeeded"),
        (lambda b: setattr(b, "status", "running"), "ingestion_not_succeeded"),
        (
            lambda b: setattr(
                b,
                "items",
                [i for i in b.items if i.meta.get("statement") != "cash_flow"],
            ),
            "statement_missing",
        ),
        (
            lambda b: setattr(
                b, "items", [i for i in b.items if i.kind != "report_file"]
            ),
            "report_missing",
        ),
    ],
)
async def test_batches_that_cannot_make_a_dataset(prepare, code):
    batches, store = FakeBatches(), FakeDatasets()
    prepare(batches)
    with pytest.raises(DatasetBuildError) as caught:
        await build_service(batches, store).build("batch-1")
    assert caught.value.code == code and store.saved == []


async def test_unknown_batch_and_code_without_a_batch():
    service = build_service(FakeBatches("failed"), FakeDatasets())
    with pytest.raises(DatasetBuildError, match="ingestion_not_found"):
        await service.build("batch-2")
    with pytest.raises(DatasetBuildError, match="no_succeeded_ingestion"):
        await service.build_latest("600009")
