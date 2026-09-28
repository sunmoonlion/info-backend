"""跨年核对：同一年的数，当年年报的本期列与下一年年报的上期列（0008-info-statements）。"""

from __future__ import annotations

import gzip
import json
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from app.application.securities.report_extraction import (
    cross_check,
    extract_statements,
    judge,
)
from app.domain.securities.report_statements import (
    AGREE,
    AMBIGUOUS,
    DIFFERENT,
    ONLY_EARLIER,
    ONLY_LATER,
    PAIRED_BY_LABEL,
    PAIRED_BY_VALUE,
    SIGN_ONLY,
    SUSPECT_TRUNCATED,
    SUSPECT_UNIT,
    UNIT_ON_STATEMENT,
    ExtractedRow,
    ExtractedStatement,
    PageWords,
    Word,
)

FIXTURES = Path(__file__).parent / "fixtures" / "securities" / "report_words"


def statement(rows, unit="元", name="income_statement") -> ExtractedStatement:
    built = tuple(
        ExtractedRow(
            90,
            label,
            None if current is None else Decimal(current),
            None if prior is None else Decimal(prior),
        )
        for label, current, prior in rows
    )
    return ExtractedStatement(name, 90, unit, UNIT_ON_STATEMENT, False, built, {})


def verdicts(result) -> dict[str, str]:
    return {f"{i.label}#{i.occurrence}": i.verdict for i in result.items}


# ------------------------------------------------------------------ 两个数的关系


@pytest.mark.parametrize(
    ("earlier", "later", "tolerance", "verdict"),
    [
        ("21650419000", "21650419000", "1000", AGREE),
        ("21650419000", "21650419400", "1000", AGREE),
        ("100.00", "100.99", "1", AGREE),
        ("100.00", "101.01", "1", DIFFERENT),
        ("-250", "250", "1", SIGN_ONLY),
        ("419000", "21650419000", "1000", SUSPECT_TRUNCATED),
        ("21650419000", "419000", "1000", SUSPECT_TRUNCATED),
        ("7382", "85127382", "1", SUSPECT_TRUNCATED),
        ("27440000.00", "8027440000.00", "1", SUSPECT_TRUNCATED),
        ("194571000000", "194571000", "1000", SUSPECT_UNIT),
        ("194571000", "194571000000", "1000", SUSPECT_UNIT),
        ("1945710000", "194571", "1", SUSPECT_UNIT),
        ("1945710000000", "1945710", "1", SUSPECT_UNIT),
        ("6152331.77", "2946593976.34", "1", DIFFERENT),
        ("3700000000", "8100000000", "1", DIFFERENT),
        ("5", "12345", "1", DIFFERENT),
        ("0", "500", "1", DIFFERENT),
    ],
)
def test_how_two_figures_for_the_same_year_relate(earlier, later, tolerance, verdict):
    assert judge(Decimal(earlier), Decimal(later), Decimal(tolerance)) == verdict


# ------------------------------------------------------------------ 配对


def test_the_same_items_agree_across_two_reports():
    earlier = statement(
        [("一、营业总收入", "5000.00", "4000.00"), ("四、净利润", "750.00", "600.00")]
    )
    later = statement(
        [("一、营业总收入", "6000.00", "5000.00"), ("四、净利润", "900.00", "750.00")]
    )
    result = cross_check(earlier, later, fiscal_year=2024)
    assert (result.statement, result.fiscal_year) == ("income_statement", 2024)
    assert verdicts(result) == {"营业总收入#1": AGREE, "净利润#1": AGREE}
    assert result.compared == 2 and result.suspects == ()
    item = result.items[0]
    assert (item.earlier, item.later, item.paired_by) == (
        Decimal("750.00"),
        Decimal("750.00"),
        PAIRED_BY_LABEL,
    )


def test_labels_are_matched_after_normalization():
    earlier = statement([("减：所得税费用七、76", "250.00", "200.00")])
    later = statement([("减：所得税费用（六十一）", "300.00", "250.00")])
    assert verdicts(cross_check(earlier, later, fiscal_year=2024)) == {
        "所得税费用#1": AGREE
    }


def test_reports_in_different_units_are_compared_in_yuan():
    """宁德时代 2020 年年报以元列示，2021 年年报改成万元。"""
    earlier = statement([("营业收入", "50319487697.12", None)], unit="元")
    later = statement([("营业收入", "13035579.64", "5031948.77")], unit="万元")
    result = cross_check(earlier, later, fiscal_year=2020)
    assert verdicts(result) == {"营业收入#1": AGREE}
    assert result.items[0].later == Decimal("50319487700.00")
    off = statement([("营业收入", "13035579.64", "5031960.00")], unit="万元")
    assert verdicts(cross_check(earlier, off, fiscal_year=2020)) == {
        "营业收入#1": DIFFERENT
    }


def test_per_share_figures_are_not_multiplied_by_the_unit():
    """比亚迪 2021 年以元列示、2022 年以千元列示，每股收益两年都是元每股。"""
    earlier = statement([("基本每股收益", "1.06", None)], unit="元")
    later = statement([("基本每股收益", "5.71", "1.06")], unit="千元")
    result = cross_check(earlier, later, fiscal_year=2021)
    assert verdicts(result) == {"基本每股收益#1": AGREE}
    assert result.items[0].later == Decimal("1.06")


def test_rows_with_the_same_name_are_paired_in_order():
    earlier = statement([("其他", "10.00", None), ("其他", "20.00", None)])
    later = statement([("其他", None, "10.00"), ("其他", None, "25.00")])
    assert verdicts(cross_check(earlier, later, fiscal_year=2024)) == {
        "其他#1": AGREE,
        "其他#2": DIFFERENT,
    }


def test_a_different_number_of_rows_with_the_same_name_is_not_guessed():
    earlier = statement([("其他", "10.00", None), ("其他", "20.00", None)])
    later = statement([("其他", None, "20.00")])
    result = cross_check(earlier, later, fiscal_year=2024)
    assert [i.verdict for i in result.items] == [AMBIGUOUS] * 3
    assert result.compared == 0


def test_rows_without_a_name_are_never_paired_by_position():
    earlier = statement([("", "9423183.00", None)])
    later = statement([("", None, "1080.00")])
    result = cross_check(earlier, later, fiscal_year=2021)
    assert [i.verdict for i in result.items] == [AMBIGUOUS, AMBIGUOUS]


def test_items_on_one_side_only_are_listed():
    earlier = statement(
        [("信用减值损失", "12.00", None), ("营业收入", "5000.00", None)]
    )
    later = statement([("营业收入", None, "5000.00"), ("数据资源", None, "7.00")])
    assert verdicts(cross_check(earlier, later, fiscal_year=2024)) == {
        "信用减值损失#1": ONLY_EARLIER,
        "营业收入#1": AGREE,
        "数据资源#1": ONLY_LATER,
    }


def test_blank_cells_are_not_compared():
    earlier = statement([("营业收入", None, "4000.00")])
    later = statement([("营业收入", "6000.00", None)])
    assert cross_check(earlier, later, fiscal_year=2024).items == ()


def test_the_two_statements_must_be_the_same_one():
    with pytest.raises(ValueError, match="same statement"):
        cross_check(
            statement([], name="income_statement"),
            statement([], name="cash_flow"),
            fiscal_year=2024,
        )


# ------------------------------------------------------------------ 按数配对


def test_a_label_broken_at_different_places_is_paired_by_its_value():
    """美的集团：同一个科目名在两份年报里折行的位置不同。"""
    earlier = statement(
        [("归属于母公司股东的其他综合收益的", "-1087461", None)], unit="千元"
    )
    later = statement(
        [("归属于母公司股东的其他综合收益的税后", None, "-1087461")], unit="千元"
    )
    result = cross_check(earlier, later, fiscal_year=2018)
    assert len(result.items) == 1
    item = result.items[0]
    assert (item.verdict, item.paired_by) == (AGREE, PAIRED_BY_VALUE)
    assert item.label == "归属于母公司股东的其他综合收益的"
    assert item.later_label == "归属于母公司股东的其他综合收益的税后"
    assert result.compared == 1


def test_a_value_that_occurs_twice_is_not_used_for_pairing():
    earlier = statement([("甲", "1023000.00", None), ("乙", "1023000.00", None)])
    later = statement([("丙", None, "1023000.00"), ("丁", None, "1023000.00")])
    result = cross_check(earlier, later, fiscal_year=2018)
    assert (
        sorted(i.verdict for i in result.items) == [ONLY_EARLIER] * 2 + [ONLY_LATER] * 2
    )


def test_small_values_are_not_used_for_pairing():
    earlier = statement(
        [("基本每股收益人民币元", "3.08", None), ("甲", "999.00", None)]
    )
    later = statement([("基本每股收益", None, "3.08"), ("乙", None, "999.00")])
    result = cross_check(earlier, later, fiscal_year=2018)
    assert all(i.paired_by == PAIRED_BY_LABEL for i in result.items)
    assert result.compared == 0


def test_pairing_by_value_never_overrides_a_pair_made_by_label():
    earlier = statement([("营业收入", "5000.00", None), ("甲", "6000.00", None)])
    later = statement([("营业收入", None, "6000.00"), ("乙", None, "5000.00")])
    result = cross_check(earlier, later, fiscal_year=2024)
    by = verdicts(result)
    assert by["营业收入#1"] == DIFFERENT
    assert by["甲#1"] == ONLY_EARLIER and by["乙#1"] == ONLY_LATER


# ------------------------------------------------------------------ 真实年报


def extracted(name: str):
    body = json.loads(gzip.decompress((FIXTURES / f"{name}.json.gz").read_bytes()))
    return extract_statements(
        [
            PageWords(p["page"], tuple(Word(*w) for w in p["words"]))
            for p in body["pages"]
        ]
    )


@pytest.mark.parametrize(
    ("earlier", "later", "year", "expected"),
    [
        (
            "000858-2024",
            "000858-2025",
            2024,
            # 表：（两边都有的数，其中按数配上的，配不上的）
            {
                "balance_sheet": (45, 0, 0),
                "income_statement": (29, 3, 4),
                "cash_flow": (28, 6, 0),
            },
        ),
        (
            "000333-2018",
            "000333-2019",
            2018,
            {
                "balance_sheet": (54, 3, 9),
                "income_statement": (40, 9, 8),
                "cash_flow": (40, 11, 0),
            },
        ),
    ],
)
def test_two_consecutive_real_reports_agree_on_every_figure(
    earlier, later, year, expected
):
    first, second = extracted(earlier), extracted(later)
    for name, (compared, by_value, unpaired) in expected.items():
        result = cross_check(
            first.statements[name], second.statements[name], fiscal_year=year
        )
        assert result.compared == compared
        assert result.count(AGREE) == compared
        assert result.suspects == ()
        assert result.count(DIFFERENT) == result.count(SIGN_ONLY) == 0
        assert sum(1 for i in result.items if i.paired_by == PAIRED_BY_VALUE) == (
            by_value
        )
        assert len(result.items) - result.compared == unpaired


def test_the_figures_the_probe_misread_would_have_been_caught():
    """美的集团 2018 年：探针把 21,650,419 读成了 419。与 2019 年年报的上期数一对就发现。"""
    first, second = extracted("000333-2018"), extracted("000333-2019")
    income = first.statements["income_statement"]
    rows = tuple(
        replace(r, current=Decimal("419")) if r.label == "四、净利润" else r
        for r in income.rows
    )
    assert rows != income.rows
    result = cross_check(
        replace(income, rows=rows),
        second.statements["income_statement"],
        fiscal_year=2018,
    )
    assert [(i.label, i.earlier, i.later) for i in result.suspects] == [
        ("净利润", Decimal("419000"), Decimal("21650419000"))
    ]
    assert result.suspects[0].verdict == SUSPECT_TRUNCATED


def test_a_misread_unit_shows_on_every_row():
    """比亚迪 2018 年的资产负债表以元列示，探针认成了千元。"""
    first = extracted("002594-2018").statements["balance_sheet"]
    wrong = replace(first, unit="千元")
    later = replace(
        first,
        rows=tuple(ExtractedRow(r.page, r.label, None, r.current) for r in first.rows),
    )
    result = cross_check(wrong, later, fiscal_year=2018)
    assert result.compared > 50
    assert (
        result.count(SUSPECT_UNIT) + result.count(SUSPECT_TRUNCATED) == result.compared
    )
    assert cross_check(first, later, fiscal_year=2018).count(AGREE) == result.compared
