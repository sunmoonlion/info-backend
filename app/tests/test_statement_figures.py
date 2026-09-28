"""「主要会计数据」表认不出来时，从年报的合并报表取关键数字（0008-info-statements 段六的后续）。"""

from __future__ import annotations

import gzip
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from app.application.securities.dataset.basis import assign_basis
from app.application.securities.dataset.quality import run_checks
from app.application.securities.dataset.statement_figures import (
    figures_from_statements,
)
from app.application.securities.report_extraction import extract_statements
from app.domain.securities.dataset import OfficialFigure, StatementRow
from app.domain.securities.report_statements import PageWords, Word

FIXTURES = Path(__file__).parent / "fixtures" / "securities" / "report_words"
EXPECTED = json.loads((FIXTURES / "expected.json").read_text())


def pages_of(name: str) -> list[PageWords]:
    body = json.loads(gzip.decompress((FIXTURES / f"{name}.json.gz").read_bytes()))
    return [
        PageWords(p["page"], tuple(Word(*w) for w in p["words"])) for p in body["pages"]
    ]


def parsed(name: str, reference=None, **changes):
    code, year = name.split("-")
    about = {
        "report_fiscal_year": int(year),
        "title": f"{code} {year}年年度报告",
        "disclosed_date": f"{int(year) + 1}-04-28",
        "revised": False,
        "reference_in_yuan": reference or {},
    }
    about.update(changes)
    return figures_from_statements(extract_statements(pages_of(name)), **about)


def reference_of(name: str) -> dict[str, dict[str, Decimal]]:
    return {
        statement: {k: Decimal(v) for k, v in values.items()}
        for statement, values in EXPECTED[name]["reference_in_yuan"].items()
    }


def test_key_figures_come_from_the_consolidated_statements():
    got = parsed("000858-2025")
    assert got.problem is None and got.page == 51
    by_year = {}
    for f in got.figures:
        by_year.setdefault((f.fiscal_year, f.basis), set()).add(f.item)
    items = {
        "operate_income",
        "total_profit",
        "parent_netprofit",
        "netcash_operate",
        "total_parent_equity",
        "total_assets",
    }
    assert by_year == {(2025, "原始披露"): items, (2024, "比较数"): items}
    assets = next(
        f for f in got.figures if f.item == "total_assets" and f.fiscal_year == 2025
    )
    expected = EXPECTED["000858-2025"]["statements"]["balance_sheet"]["values"]
    assert assets.value == float(expected["current"]["total_assets"])
    assert (assets.column_label, assets.precision) == ("合并资产负债表 本期", 1.0)
    assert assets.source_report == "000858 2025年年度报告"
    assert (assets.report_fiscal_year, assets.disclosed_date) == (2025, "2026-04-28")
    assert assets.page >= 51 and not assets.revised_report


def test_the_adjusted_net_profit_is_not_in_the_statements():
    got = parsed("000858-2025")
    assert "deduct_parent_netprofit" not in {f.item for f in got.figures}


def test_a_report_in_thousands_gives_yuan_and_says_how_precise_it_is():
    got = parsed("688981-2023")
    assert got.problem is None
    assets = next(
        f for f in got.figures if f.item == "total_assets" and f.fiscal_year == 2023
    )
    printed = EXPECTED["688981-2023"]["statements"]["balance_sheet"]["values"]
    assert assets.value == float(printed["current"]["total_assets"]) * 1000
    assert {f.precision for f in got.figures} == {1000.0}


def test_a_revised_report_marks_its_figures():
    got = parsed("000858-2025", revised=True)
    assert all(f.revised_report for f in got.figures)


def test_statements_that_do_not_balance_are_not_used():
    """美的集团 2018 年：所得税印成负数、一个科目名是乱码，三张表都过不了。"""
    got = parsed("000333-2018")
    assert got.figures == () and got.page is None
    assert got.problem == (
        "statements_unusable（balance_sheet:unbalanced；"
        "income_statement:unbalanced；cash_flow:not_reconciled）"
    )


def test_statements_that_were_refused_are_named():
    got = parsed("601899-2025")
    assert got.problem == (
        "statements_unusable（balance_sheet:not_extracted；"
        "income_statement:not_extracted；cash_flow:not_extracted）"
    )


def test_one_refused_statement_does_not_stop_the_others():
    """万科 2018 年的资产负债表有三列数字，被拒绝；另两张表照常用。

    它的利润表只印了「营业总收入」，没有单独的「营业收入」一行：不拿前者顶替后者。
    """
    got = parsed("000002-2018")
    assert got.problem is None
    assert {f.item for f in got.figures} == {
        "total_profit",
        "parent_netprofit",
        "netcash_operate",
    }


def test_a_unit_that_the_statement_does_not_state_needs_a_reference():
    """比亚迪 2024 年：报表页上不写单位，只在别处声明过一次。"""
    without = parsed("002594-2024")
    assert without.problem == (
        "statements_unusable（balance_sheet:unit_no_reference；"
        "income_statement:unit_no_reference；cash_flow:unit_no_reference）"
    )
    confirmed = parsed("002594-2024", reference_of("002594-2024"))
    assert confirmed.problem is None
    assert {f.precision for f in confirmed.figures} == {1000.0}
    wrong = {
        name: {k: v / 1000 for k, v in values.items()}
        for name, values in reference_of("002594-2024").items()
    }
    refused = parsed("002594-2024", wrong)
    assert refused.figures == () and "unit_unit_mismatch" in (refused.problem or "")


# ------------------------------------------------------------------ 与口径判定、质量检查接上


def row(statement: str, year: int, **values) -> StatementRow:
    return StatementRow(
        statement=statement,
        security_code="688981",
        report_date=f"{year}-12-31",
        report_type="年报",
        fiscal_year=year,
        aggregator_notice_date=None,
        currency="CNY",
        values=values,
    )


def third_party(figures, year: int, shift: float = 0.0):
    """按年报的数造第三方的报表行；shift 是第三方多出来的零头。"""
    from app.domain.securities.financial_catalog import KEY_ITEMS

    tables = {i.item: (i.table, i.field) for i in KEY_ITEMS}
    grouped: dict[str, dict[str, float]] = {}
    for f in figures:
        if f.fiscal_year == year and f.basis == "原始披露":
            table, name = tables[f.item]
            grouped.setdefault(table, {})[name] = f.value + shift
    return {t: [row(t, year, **v)] for t, v in grouped.items()}


@pytest.mark.parametrize(
    ("shift", "basis"),
    [(0.0, "原始披露"), (499.37, "原始披露"), (-999.0, "原始披露"), (1500.0, "未核实")],
)
def test_agreement_is_judged_at_the_precision_the_report_prints(shift, basis):
    """年报以千元列示：第三方的数带着零头，差在一千元以内就是同一个数。"""
    figures = list(parsed("688981-2023").figures)
    rows = third_party(figures, 2023, shift)
    decided = assign_basis(rows, figures)
    assert decided[2023][0] == basis
    assert rows["balance_sheet"][0].basis == basis


def test_figures_in_yuan_are_still_judged_to_one_yuan():
    figures = list(parsed("000858-2025").figures)
    assert assign_basis(third_party(figures, 2025, 0.9), figures)[2025][0] == "原始披露"
    assert assign_basis(third_party(figures, 2025, 1.5), figures)[2025][0] == "未核实"


def test_the_official_check_uses_the_same_precision():
    from datetime import date

    figures = list(parsed("688981-2023").figures)
    rows = third_party(figures, 2023, 499.0)
    assign_basis(rows, figures)
    report = run_checks(rows, figures, [], today=date(2024, 6, 1))
    official = next(c for c in report.checks if c.check_id == "Q-OFFICIAL")
    assert official.passed and official.checked == 6
    rows["balance_sheet"][0].values["total_assets"] += 5000.0
    report = run_checks(rows, figures, [], today=date(2024, 6, 1))
    official = next(c for c in report.checks if c.check_id == "Q-OFFICIAL")
    assert not official.passed and official.violations[0]["item"] == "total_assets"


def test_figures_without_a_stated_precision_behave_as_before():
    figure = OfficialFigure(
        2023, "total_assets", 1.0, "原始披露", "x", "r", 2023, "d", 1, False
    )
    assert figure.precision == 1.0
    assert replace(figure, precision=1000.0).precision == 1000.0
