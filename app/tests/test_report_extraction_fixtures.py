"""抽取核心在真实年报上的结果（STMT-05、STMT-06）。

夹具是从巨潮的年度报告（法定披露）里裁出来的词和坐标：只留三张合并报表所在的页，
文字一个没改。每份夹具代表一种版式，见 fixtures/securities/report_words/README.md。
期望值里的关键数字在 2026-09-28 的探针里与第三方数据逐项比对过。
"""

from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from app.application.securities.report_extraction import (
    check_unit,
    extract_statements,
    reconcile,
    resolve,
    values_of,
)
from app.domain.securities.report_statements import PageWords, Word

FIXTURES = Path(__file__).parent / "fixtures" / "securities" / "report_words"
EXPECTED = json.loads((FIXTURES / "expected.json").read_text())


def pages_of(name: str) -> list[PageWords]:
    body = json.loads(gzip.decompress((FIXTURES / f"{name}.json.gz").read_bytes()))
    return [
        PageWords(p["page"], tuple(Word(*w) for w in p["words"])) for p in body["pages"]
    ]


def fingerprint(rows) -> str:
    text = json.dumps(
        [
            [
                r.page,
                r.label,
                None if r.current is None else str(r.current),
                None if r.prior is None else str(r.prior),
            ]
            for r in rows
        ],
        ensure_ascii=False,
    )
    return hashlib.sha256(text.encode()).hexdigest()


def test_every_layout_in_the_design_has_a_fixture():
    layouts = {layout for one in EXPECTED.values() for layout in one["layouts"]}
    assert layouts == {
        "standard",
        "numbered_title",
        "unit_thousand_no_decimals",
        "unit_ten_thousand",
        "side_by_side",
        "period_in_title_and_continued",
        "split_numbers",
        "costs_as_negatives",
        "unit_note_above_title",
        "unit_declared_elsewhere",
        "wrapped_labels",
        "columns_shift_between_pages",
        "beijing_exchange",
        "broken_amount_refused",
        "three_columns_refused",
        "index_page_of_titles",
        "scanned_statements_refused",
    }


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_a_real_report_gives_the_expected_statements(name):
    expected = EXPECTED[name]
    found = extract_statements(pages_of(name))
    assert [[p.code, p.statement] for p in found.problems] == expected["problems"]
    assert sorted(found.statements) == sorted(expected["statements"])
    for statement, want in expected["statements"].items():
        got = found.statements[statement]
        assert (got.unit, got.unit_source, got.side_by_side, got.start_page) == (
            want["unit"],
            want["unit_source"],
            want["side_by_side"],
            want["start_page"],
        )
        numbers = [r for r in got.rows if r.current is not None or r.prior is not None]
        assert len(numbers) == want["rows_with_numbers"]
        assert fingerprint(got.rows) == want["rows_sha256"]
        lines = resolve(statement, got.rows)
        assert sorted(lines.lines) == want["fields"]
        assert list(lines.ambiguous) == want["ambiguous"]
        assert (
            sum(1 for x in lines.lines.values() if x.label_lines > 1)
            == (want["wrapped"])
        )
        checked, broken = reconcile(got, lines)
        assert checked == want["checked"]
        assert [
            [b.rule_id, b.column, str(b.residual), list(b.balanced_if_flipped)]
            for b in broken
        ] == want["broken"]
        for column, values in want["values"].items():
            mine = values_of(lines, column)
            assert {k: str(mine[k]) for k in values} == values


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_the_unit_agrees_with_the_reference(name):
    """STMT-08：参照数是第三方数据里同一年的资产总计、净利润、期末现金，以元计。"""
    expected = EXPECTED[name]
    found = extract_statements(pages_of(name))
    for statement, reference in expected["reference_in_yuan"].items():
        got = found.statements[statement]
        lines = resolve(statement, got.rows)
        verdict, _ = check_unit(
            got, lines, {k: Decimal(v) for k, v in reference.items()}
        )
        assert verdict == expected["unit_verdict"][statement]


def test_the_unit_the_probe_misread_is_caught_by_the_reference():
    """STMT-08：比亚迪 2018 年的资产负债表以元列示，探针认成了千元，抽出的数大了一千倍。

    勾稽发现不了（整张表同比例放大仍然是平的）；与参照数一比就发现了。
    """
    found = extract_statements(pages_of("002594-2018"))
    right = found.statements["balance_sheet"]
    lines = resolve("balance_sheet", right.rows)
    reference = {
        k: Decimal(v)
        for k, v in EXPECTED["002594-2018"]["reference_in_yuan"][
            "balance_sheet"
        ].items()
    }
    assert (right.unit, right.unit_source) == ("元", "statement_page")
    assert check_unit(right, lines, reference)[0] == "confirmed"
    misread = replace(right, unit="千元")
    assert reconcile(misread, lines) == reconcile(right, lines)
    assert reconcile(misread, lines)[1] == ()
    verdict, each = check_unit(misread, lines, reference)
    assert verdict == "unit_mismatch" and each == {"total_assets": "unit_mismatch"}


def test_numbers_split_in_the_pdf_are_whole_again():
    """美的集团 2018 年：「21,650,419」在 PDF 里是两个词，探针只读到了 419。"""
    found = extract_statements(pages_of("000333-2018"))
    income = found.statements["income_statement"]
    lines = resolve("income_statement", income.rows)
    assert values_of(lines, "current")["netprofit"] == Decimal("21650419")
    assert values_of(lines, "current")["parent_netprofit"] == Decimal("20230779")
    going = next(r for r in income.rows if r.label == "持续经营净利润")
    assert going.current == Decimal("21650419")
    broken = reconcile(income, lines)[1]
    assert [(b.rule_id, b.balanced_if_flipped) for b in broken] == [
        ("R07", ("income_tax",)),
        ("R07", ("income_tax",)),
    ]


def test_statements_printed_as_pictures_give_no_numbers():
    """紫金矿业 2025 年：财务报表那十几页是图片，一个词都没有。"""
    pages = pages_of("601899-2025")
    assert sum(1 for p in pages if not p.words) >= 15
    found = extract_statements(pages)
    assert found.statements == {} and found.pages_read == len(pages)


def test_fixtures_say_where_they_come_from():
    for name in EXPECTED:
        body = json.loads(gzip.decompress((FIXTURES / f"{name}.json.gz").read_bytes()))
        source = body["source"]
        assert source["source"] == "cninfo"
        assert source["copyright_status"] == "public_disclosure"
        assert f"{source['security_code']}-{source['fiscal_year']}" == name
        assert len(source["pdf_sha256"]) == 64 and source["announcement_id"].isdecimal()
