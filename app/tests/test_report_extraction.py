"""年报三张合并报表的抽取核心（0008-info-statements 段七）：版面、抽取、认科目、勾稽、量级。

这里的页面是按真实年报的排法造出来的，用来把每条规则单独钉住；
真实年报裁出来的夹具在 test_report_extraction_fixtures.py。
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from app.application.securities.report_extraction import (
    check_unit,
    extract_statements,
    magnitude,
    normalize,
    reconcile,
    resolve,
    values_of,
)
from app.application.securities.report_extraction.layout import (
    amount,
    columns,
    is_number,
    lines_of,
    statement_of,
    text_of,
    title_of,
    unit_in,
)
from app.domain.securities.dataset import DatasetBuildError
from app.domain.securities.report_statements import (
    UNIT_ABOVE_TITLE,
    UNIT_DECLARED_ELSEWHERE,
    UNIT_ON_STATEMENT,
    ExtractedRow,
    ExtractedStatement,
    PageWords,
    Word,
)
from app.infrastructure.securities.pdf_words import PdfPlumberWordsReader

LEFT, NOTE, CURRENT, PRIOR = 72.0, 300.0, 430.0, 540.0
ONE_PAGE = Path(__file__).parent / "fixtures" / "securities" / "dataset"


def word(text: str, right: float, top: float, *, left: float | None = None) -> Word:
    width = 5.0 * len(text)
    if left is not None:
        return Word(text, left, left + width, top)
    return Word(text, right - width, right, top)


def line(top, label=None, current=None, prior=None, note=None, extra=()):
    words = []
    if label:
        words.append(word(label, 0, top, left=LEFT))
    if note:
        words.append(word(note, NOTE, top))
    if current:
        words.append(word(current, CURRENT, top))
    if prior:
        words.append(word(prior, PRIOR, top))
    for text, right in extra:
        words.append(word(text, right, top))
    return words


def page(number: int, *lines) -> PageWords:
    words = []
    for index, make in enumerate(lines):
        words.extend(make(100.0 + 14.0 * index))
    return PageWords(number, tuple(words))


def row(label=None, current=None, prior=None, note=None, extra=()):
    return lambda top: line(top, label, current, prior, note, extra)


BALANCE = [
    row("流动资产："),
    row("货币资金", "1,200.00", "1,000.00", note="七、1"),
    row("流动资产合计", "3,000.00", "2,500.00"),
    row("非流动资产合计", "7,000.00", "6,500.00"),
    row("资产总计", "10,000.00", "9,000.00"),
    row("流动负债合计", "2,000.00", "1,800.00"),
    row("非流动负债合计", "1,000.00", "1,200.00"),
    row("负债合计", "3,000.00", "3,000.00"),
    row("归属于母公司所有者权益合计", "6,500.00", "5,600.00"),
    row("少数股东权益", "500.00", "400.00"),
    row("所有者权益合计", "7,000.00", "6,000.00"),
    row("负债和所有者权益总计", "10,000.00", "9,000.00"),
]
INCOME = [
    row("一、营业总收入", "5,000.00", "4,000.00"),
    row("三、利润总额（亏损总额以“－”号填列）", "1,000.00", "800.00"),
    row("减：所得税费用", "250.00", "200.00", note="七、76"),
    row("四、净利润（净亏损以“－”号填列）", "750.00", "600.00"),
    row("1.归属于母公司股东的净利润", "700.00", "560.00"),
    row("2.少数股东损益", "50.00", "40.00"),
]
CASH = [
    row("经营活动产生的现金流量净额", "900.00", "700.00"),
    row("投资活动产生的现金流量净额", "-600.00", "-500.00"),
    row("筹资活动产生的现金流量净额", "(100.00)", "(50.00)"),
    row("四、汇率变动对现金及现金等价物的影响", "10.00", "5.00"),
    row("五、现金及现金等价物净增加额", "210.00", "155.00"),
    row("加：期初现金及现金等价物余额", "1,000.00", "845.00"),
    row("六、期末现金及现金等价物余额", "1,210.00", "1,000.00"),
]


def report(
    balance=BALANCE,
    income=INCOME,
    cash=CASH,
    titles=("合并资产负债表", "合并利润表", "合并现金流量表"),
    unit="单位:元 币种:人民币",
    ends=("母公司资产负债表", "母公司利润表", "母公司现金流量表"),
):
    pages, number = [], 80
    for title, body, end in zip(titles, (balance, income, cash), ends, strict=True):
        head = [row(title), row("2025年12月31日")]
        if unit:
            head.append(row(unit))
        head.append(row("项目附注期末余额期初余额"))
        pages.append(page(number, *head, *body))
        pages.append(page(number + 1, row(end), row("货币资金", "9.00", "8.00")))
        number += 2
    return pages


def extracted(**changes):
    return extract_statements(report(**changes))


# ------------------------------------------------------------------ 版面


@pytest.mark.parametrize(
    ("printed", "title"),
    [
        ("合并资产负债表", "合并资产负债表"),
        ("1、合并资产负债表", "合并资产负债表"),
        ("（一）合并利润表", "合并利润表"),
        ("二、合并现金流量表", "合并现金流量表"),
        ("2024年度合并及公司利润表", "合并及公司利润表"),
        ("2024年12月31日合并及公司资产负债表", "合并及公司资产负债表"),
        ("合并现金流量表(续)", "合并现金流量表"),
        ("合并资产负债表（续）", "合并资产负债表"),
    ],
)
def test_titles_are_recognized_in_their_printed_variants(printed, title):
    assert title_of(printed) == title


@pytest.mark.parametrize(
    "sentence",
    [
        "后附的合并资产负债表和合并利润表公允反映了",
        "2025年度，公司实现营业收入",
        "1、货币资金",
        "详见合并财务报表项目注释之合并资产负债表项目",
    ],
)
def test_sentences_that_mention_a_statement_are_not_titles(sentence):
    assert title_of(sentence) == sentence
    assert statement_of(title_of(sentence)) == (None, False)


def test_which_statement_a_title_means():
    assert statement_of("合并资产负债表") == ("balance_sheet", False)
    assert statement_of("合并及公司利润表") == ("income_statement", True)
    assert statement_of("合并及母公司现金流量表") == ("cash_flow", True)
    assert statement_of("母公司资产负债表") == (None, False)
    assert statement_of("合并所有者权益变动表") == (None, False)


@pytest.mark.parametrize(
    ("text", "unit"),
    [
        ("单位:元币种:人民币", "元"),
        ("单位：千元", "千元"),
        ("编制单位：某某股份有限公司单位：人民币万元", "万元"),
        ("金额单位为人民币千元", "千元"),
        ("人民币百万元", "百万元"),
        ("(除特别注明外，金额单位为人民币元)", "元"),
        ("货币资金", None),
    ],
)
def test_units_as_they_are_printed(text, unit):
    assert unit_in(text) == unit


@pytest.mark.parametrize(
    ("printed", "value"),
    [
        ("1,234,567.89", "1234567.89"),
        ("-1,234.50", "-1234.50"),
        ("(1,234.50)", "-1234.50"),
        ("（1,234.50）", "-1234.50"),
        ("－88", "-88"),
        ("—88.10", "-88.10"),
        ("0.00", "0.00"),
        ("12345", "12345"),
    ],
)
def test_amounts_are_read_exactly(printed, value):
    assert is_number(printed)
    assert amount(printed) == Decimal(value)
    assert str(amount(printed)) == value


@pytest.mark.parametrize(
    "text",
    [
        "七、1",
        "-",
        "—",
        "12.5%",
        "2025年",
        "1,23",
        "不适用",
        "1.2.3",
        "",
        "0,419",
        "(0,419)",
    ],
)
def test_what_is_not_an_amount(text):
    assert not is_number(text)


def test_words_at_the_same_height_make_one_line_left_to_right():
    words = [
        word("1,000.00", PRIOR, 100.4),
        word("货币资金", 0, 100.0, left=LEFT),
        word("1,200.00", CURRENT, 101.2),
        word("存货", 0, 114.0, left=LEFT),
    ]
    assert [text_of(found) for found in lines_of(words)] == [
        "货币资金1,200.001,000.00",
        "存货",
    ]


def test_two_columns_are_found_by_their_right_edges():
    edges = [CURRENT + d for d in (0, 0.3, -0.4, 0.1)] + [
        PRIOR,
        PRIOR + 0.2,
        PRIOR - 0.2,
    ]
    found, why = columns(edges)
    assert why is None and found is not None
    assert [round(x) for x in found] == [430, 540]


def test_a_stray_amount_in_running_text_does_not_become_a_column():
    edges = [CURRENT] * 10 + [PRIOR] * 9 + [200.0]
    found, why = columns(edges)
    assert why is None and found is not None
    assert [round(x) for x in found] == [430, 540]


@pytest.mark.parametrize(
    ("edges", "wanted", "why"),
    [
        ([CURRENT] * 8, 2, "one_column"),
        ([CURRENT] * 8 + [PRIOR] * 8 + [330.0] * 6, 2, "more_than_two_columns"),
        ([CURRENT] * 8 + [PRIOR] * 8, 4, "expected_four_columns_found_2"),
        (
            [250.0] * 8 + [340.0] * 8 + [CURRENT] * 8 + [PRIOR] * 8 + [160.0] * 8,
            4,
            "expected_four_columns_found_5",
        ),
    ],
)
def test_column_layouts_that_are_not_recognized_say_why(edges, wanted, why):
    assert columns(edges, wanted) == (None, why)


def test_side_by_side_takes_the_two_columns_on_the_left():
    edges = [250.0] * 9 + [340.0] * 8 + [CURRENT] * 8 + [PRIOR] * 7
    found, why = columns(edges, 4)
    assert why is None and found == (250.0, 340.0)


# ------------------------------------------------------------------ 抽取


def test_three_statements_are_extracted_with_every_row():
    got = extracted()
    assert got.complete and got.problems == ()
    assert list(got.statements) == ["balance_sheet", "income_statement", "cash_flow"]
    balance = got.statements["balance_sheet"]
    assert (balance.start_page, balance.unit, balance.unit_source) == (
        80,
        "元",
        UNIT_ON_STATEMENT,
    )
    assert not balance.side_by_side and balance.factor == 1
    assert [round(x) for x in balance.columns[80]] == [430, 540]
    numbers = [r for r in balance.rows if r.current is not None]
    assert len(numbers) == 11
    assert numbers[0] == ExtractedRow(
        80, "货币资金七、1", Decimal("1200.00"), Decimal("1000.00")
    )
    cash = got.statements["cash_flow"]
    finance = next(r for r in cash.rows if r.label.startswith("筹资"))
    assert (finance.current, finance.prior) == (Decimal("-100.00"), Decimal("-50.00"))


def test_rows_of_the_parent_statement_are_not_taken():
    got = extracted()
    for statement in got.statements.values():
        assert all(r.current != Decimal("9.00") for r in statement.rows)


def test_reading_stops_once_the_three_statements_have_ended():
    pages = [*report(), page(86, row("七、合并财务报表项目注释")), page(87, row("x"))]
    consumed = []

    def feed():
        for one in pages:
            consumed.append(one.page_number)
            yield one

    got = extract_statements(feed())
    assert got.complete
    assert consumed == [80, 81, 82, 83, 84, 85, 86] and got.pages_read == 6


@pytest.mark.parametrize(
    "titles",
    [
        ("1、合并资产负债表", "2、合并利润表", "3、合并现金流量表"),
        ("合并资产负债表", "2025年度合并利润表", "2025年度合并现金流量表"),
    ],
)
def test_title_variants_give_the_same_rows(titles):
    assert extracted(titles=titles).statements == extracted().statements


def test_a_continued_page_repeats_the_title_and_the_rows_go_on():
    pages = report()
    first = pages[0]
    head = page(
        80,
        row("合并资产负债表"),
        row("单位：元"),
        row("项目附注期末余额期初余额"),
        *BALANCE[:6],
    )
    more = page(
        81, row("合并资产负债表（续）"), row("项目附注期末余额期初余额"), *BALANCE[6:]
    )
    end = page(82, row("母公司资产负债表"))
    got = extract_statements([head, more, end, *pages[2:]])
    assert first.page_number == 80 and got.complete
    balance = got.statements["balance_sheet"]
    assert [r.label for r in balance.rows if r.current is not None] == [
        r.label
        for r in extracted().statements["balance_sheet"].rows
        if r.current is not None
    ]
    assert sorted(balance.columns) == [80, 81]
    assert all("续" not in r.label for r in balance.rows)


def test_each_page_has_its_own_column_positions():
    shifted = [
        (
            lambda top, make=make: [
                Word(w.text, w.x0 + 25, w.x1 + 25, w.top) if is_number(w.text) else w
                for w in make(top)
            ]
        )
        for make in BALANCE[6:]
    ]
    head = page(80, row("合并资产负债表"), row("单位：元"), *BALANCE[:6])
    more = page(81, *shifted)
    end = page(82, row("母公司资产负债表"))
    got = extract_statements([head, more, end, *report()[2:]])
    balance = got.statements["balance_sheet"]
    assert [round(x) for x in balance.columns[81]] == [455, 565]
    assert [r.current for r in balance.rows if r.current is not None] == [
        r.current
        for r in extracted().statements["balance_sheet"].rows
        if r.current is not None
    ]


def test_a_page_with_one_or_two_rows_borrows_the_columns_of_its_neighbour():
    head = page(80, row("合并资产负债表"), row("单位：元"), *BALANCE[:11])
    tail = page(81, *BALANCE[11:], row("母公司资产负债表"))
    got = extract_statements([head, tail, *report()[2:]])
    balance = got.statements["balance_sheet"]
    assert balance.columns[81] == balance.columns[80]
    assert balance.rows[-1].current == Decimal("10000.00")


@pytest.mark.parametrize(
    ("unit", "expected"),
    [
        ("单位：千元", "千元"),
        ("金额单位为人民币万元", "万元"),
        ("人民币百万元", "百万元"),
    ],
)
def test_the_unit_is_kept_and_the_numbers_are_left_as_printed(unit, expected):
    got = extracted(unit=unit)
    balance = got.statements["balance_sheet"]
    assert balance.unit == expected and balance.unit_source == UNIT_ON_STATEMENT
    assert balance.factor == {"千元": 1000, "万元": 10000, "百万元": 1000000}[expected]
    funds = next(r for r in balance.rows if r.label.startswith("货币资金"))
    assert funds.current == Decimal("1200.00")


def test_a_unit_stated_above_the_title_is_found():
    head = page(80, row("金额单位为人民币千元"), row("合并资产负债表"), *BALANCE)
    got = extract_statements([head, page(81, row("母公司资产负债表")), *report()[2:]])
    balance = got.statements["balance_sheet"]
    assert (balance.unit, balance.unit_source) == ("千元", UNIT_ABOVE_TITLE)


def test_the_unit_under_the_title_wins_over_one_above_it():
    """比亚迪 2018 年年报：标题上方一行说的是附注的单位，标题下面才是这张表的单位。"""
    head = page(
        80,
        row("金额单位为人民币千元"),
        row("合并资产负债表"),
        row("单位：元"),
        *BALANCE,
    )
    got = extract_statements([head, page(81, row("母公司资产负债表")), *report()[2:]])
    balance = got.statements["balance_sheet"]
    assert (balance.unit, balance.unit_source) == ("元", UNIT_ON_STATEMENT)


def test_a_statement_about_the_notes_is_not_the_unit_of_the_table_below_it():
    head = page(
        80,
        row("财务附注中报表的单位为：人民币千元"),
        row("合并资产负债表"),
        *BALANCE,
    )
    got = extract_statements([head, page(81, row("母公司资产负债表")), *report()[2:]])
    balance = got.statements["balance_sheet"]
    assert (balance.unit, balance.unit_source) == ("千元", UNIT_DECLARED_ELSEWHERE)


def test_a_unit_after_the_first_amounts_is_not_the_unit_of_the_statement():
    """利润表末尾的「基本每股收益（人民币元）」不是这张表的单位。"""
    body = [*INCOME, row("基本每股收益(人民币元)", "3.08", "2.66")]
    got = extracted(income=body, unit=None)
    assert ("unit_not_stated", "income_statement") in codes(got)
    opening = page(78, row("财务附注中报表的单位为：千元"))
    got = extract_statements([opening, *report(income=body, unit=None)])
    assert got.statements["income_statement"].unit == "千元"


def test_a_unit_from_the_page_of_an_earlier_statement_is_not_carried_over():
    pages = report()
    tail = page(
        81, row("母公司资产负债表"), row("单位：千元"), row("x", "1.00", "2.00")
    )
    income = page(82, row("合并利润表"), *INCOME)
    got = extract_statements([pages[0], tail, income, *pages[3:]])
    assert ("unit_not_stated", "income_statement") in codes(got)


def test_a_unit_declared_once_elsewhere_is_marked_as_such():
    opening = page(78, row("财务附注中报表的单位为：千元"))
    got = extract_statements([opening, *report(unit=None)])
    assert got.complete
    assert {s.unit for s in got.statements.values()} == {"千元"}
    assert {s.unit_source for s in got.statements.values()} == {UNIT_DECLARED_ELSEWHERE}


def test_side_by_side_layout_takes_the_consolidated_columns():
    def wide(make):
        def build(top):
            words = make(top)
            numbers = [w for w in words if is_number(w.text)]
            others = [w for w in words if not is_number(w.text)]
            if len(numbers) != 2:
                return words
            placed = []
            for w, right in zip(numbers, (250.0, 340.0), strict=True):
                placed.append(word(w.text, right, top))
            placed.append(word("77.00", CURRENT, top))
            placed.append(word("66.00", PRIOR, top))
            return [Word(o.text, 40.0, 40.0 + 5 * len(o.text), o.top) for o in others][
                :1
            ] + placed

        return build

    titles = ("合并及公司资产负债表", "合并及公司利润表", "合并及公司现金流量表")
    ends = ("合并及公司利润表", "合并及公司现金流量表", "合并股东权益变动表")
    pages, number = [], 80
    for title, body in zip(titles, (BALANCE, INCOME, CASH), strict=True):
        pages.append(
            page(number, row(f"2025年度{title}"), row("单位：千元"), *map(wide, body))
        )
        number += 1
    pages.append(page(number, row(ends[2])))
    got = extract_statements(pages)
    assert got.complete and got.problems == ()
    balance = got.statements["balance_sheet"]
    assert balance.side_by_side and balance.columns[80] == (250.0, 340.0)
    total = next(r for r in balance.rows if r.label == "资产总计")
    assert (total.current, total.prior) == (Decimal("10000.00"), Decimal("9000.00"))


# ------------------------------------------------------------------ 认不出就拒绝


def codes(got):
    return [(p.code, p.statement) for p in got.problems]


def test_a_statement_without_a_unit_is_refused():
    got = extracted(unit=None)
    assert got.statements == {}
    assert codes(got) == [
        ("unit_not_stated", "balance_sheet"),
        ("unit_not_stated", "income_statement"),
        ("unit_not_stated", "cash_flow"),
    ]


def test_a_missing_statement_is_reported_and_the_others_are_kept():
    pages = report()
    got = extract_statements(pages[:2] + pages[4:])
    assert sorted(got.statements) == ["balance_sheet", "cash_flow"]
    assert codes(got) == [("statement_not_found", "income_statement")]
    assert not got.complete


def test_a_statement_that_never_ends_is_refused():
    pages = report()
    got = extract_statements(pages[:5])
    assert sorted(got.statements) == ["balance_sheet", "income_statement"]
    assert codes(got) == [("section_end_not_found", "cash_flow")]


def test_a_statement_that_runs_too_long_is_refused():
    pages = report()
    filler = [page(85 + n, *CASH) for n in range(9)]
    got = extract_statements([*pages[:5], *filler, page(95, row("母公司现金流量表"))])
    assert "cash_flow" not in got.statements
    assert codes(got) == [("section_too_long", "cash_flow")]
    assert got.problems[0].page == 92


def test_three_dense_columns_are_refused_not_guessed():
    third = [
        row(None, extra=((f"{n},000.00", 330.0),)) if False else make
        for n, make in enumerate(BALANCE)
    ]
    body = [
        (lambda top, make=make: make(top) + [word("1,111.00", 330.0, top)])
        for make in third
    ]
    got = extracted(balance=body)
    assert "balance_sheet" not in got.statements
    assert codes(got) == [("more_than_two_columns", "balance_sheet")]
    assert sorted(got.statements) == ["cash_flow", "income_statement"]


def test_a_page_with_only_one_column_is_refused():
    body = [row(f"科目{n}", f"{n + 1},000.00") for n in range(6)]
    got = extracted(income=body)
    assert codes(got) == [("one_column", "income_statement")]


def test_two_amounts_in_one_column_are_refused():
    body = [
        *INCOME,
        row("其他", None, "2.00", extra=(("1.00", CURRENT - 6), ("7", CURRENT + 6))),
    ]
    got = extracted(income=body)
    assert codes(got) == [("two_amounts_in_one_column", "income_statement")]
    assert got.problems[0].page == 82 and "其他" in got.problems[0].detail


def split(top, label, pieces, prior):
    """一个数被拆成贴在一起的几个词，最后一个词的右边缘落在本期列上。"""
    words = [word(label, 0, top, left=LEFT), word(prior, PRIOR, top)]
    right = CURRENT
    for piece in reversed(pieces):
        placed = word(piece, right, top)
        words.append(placed)
        right = placed.x0 + 0.08  # 真实年报里两个碎片略有重叠
    return words


def test_a_number_split_into_touching_words_is_put_back():
    """美的集团 2018 年年报：「21,650,419」在 PDF 里是「21,65」「0,419」两个词。"""
    body = list(INCOME)
    body[3] = lambda top: split(top, "四、净利润", ("7", "50.00"), "600.00")
    body[4] = lambda top: split(
        top, "1.归属于母公司股东的净利润", ("70", "0", ".00"), "560.00"
    )
    got = extracted(income=body)
    assert got.problems == ()
    statement, found = resolved("income_statement", got)
    assert found.lines["netprofit"].row.current == Decimal("750.00")
    assert found.lines["parent_netprofit"].row.current == Decimal("700.00")
    assert reconcile(statement, found) == (4, ())


@pytest.mark.parametrize(
    "pieces",
    [("0,419",), ("21,65",), ("1,234.5", "6,789.00"), ("12,34", "5,6")],
)
def test_an_amount_that_is_still_broken_refuses_the_statement(pieces):
    """读到半个数比读不到更糟：不参与勾稽的科目没有别的办法发现。"""
    body = [*INCOME, lambda top: split(top, "其他收益", pieces, "5.00")]
    got = extracted(income=body)
    assert codes(got) == [("broken_amount", "income_statement")]
    assert got.problems[0].detail == "".join(pieces)
    assert sorted(got.statements) == ["balance_sheet", "cash_flow"]


def test_numbers_in_different_columns_are_never_joined():
    got = extracted()
    total = next(
        r for r in got.statements["balance_sheet"].rows if r.label == "资产总计"
    )
    assert (total.current, total.prior) == (Decimal("10000.00"), Decimal("9000.00"))


def test_a_statement_with_no_amounts_is_not_found():
    got = extracted(cash=[row("经营活动产生的现金流量：")])
    assert codes(got) == [("statement_not_found", "cash_flow")]


def test_a_list_of_statement_titles_is_not_taken_for_the_statements():
    """紫金矿业 2025 年年报：财务报告的扉页把各张报表的标题列了一遍。"""
    index = page(
        70,
        row("已审财务报表"),
        row("合并资产负债表"),
        row("合并利润表"),
        row("合并股东权益变动表"),
        row("合并现金流量表"),
        row("公司资产负债表"),
    )
    got = extract_statements([index, *report()])
    assert got.complete and got.problems == ()
    assert got.statements == extracted().statements
    assert got.statements["balance_sheet"].start_page == 80


def test_nothing_to_read_gives_three_problems_and_no_numbers():
    got = extract_statements([])
    assert got.statements == {} and got.pages_read == 0
    assert [p.code for p in got.problems] == ["statement_not_found"] * 3


# ------------------------------------------------------------------ 认科目


@pytest.mark.parametrize(
    ("printed", "normal"),
    [
        ("货币资金七、1", "货币资金"),
        ("货币资金（六十一）", "货币资金"),
        ("货币资金五（一）", "货币资金"),
        ("货币资金六.12", "货币资金"),
        ("货币资金5", "货币资金"),
        ("减：所得税费用", "所得税费用"),
        ("其中：营业收入", "营业收入"),
        ("三、利润总额（亏损总额以“－”号填列）", "利润总额"),
        ("（一）归属于母公司所有者的净利润", "归属于母公司股东的净利润"),
        ("1.归属于母公司股东的净利润", "归属于母公司股东的净利润"),
        ("所有者权益（或股东权益）合计", "股东权益合计"),
        ("负债和所有者权益（或股东权益）总计", "负债和股东权益总计"),
        ("实收资本（或股本）", "实收资本"),
    ],
)
def test_labels_are_normalized(printed, normal):
    assert normalize(printed) == normal


def resolved(name: str, got=None):
    statement = (got or extracted()).statements[name]
    return statement, resolve(name, statement.rows)


def test_the_rows_needed_for_reconciliation_are_recognized():
    _, balance = resolved("balance_sheet")
    assert set(balance.lines) == {
        "monetaryfunds",
        "total_current_assets",
        "total_noncurrent_assets",
        "total_assets",
        "total_current_liab",
        "total_noncurrent_liab",
        "total_liabilities",
        "total_parent_equity",
        "minority_equity",
        "total_equity",
        "total_liab_equity",
    }
    assert balance.ambiguous == () and balance.unmatched == ()
    _, income = resolved("income_statement")
    assert {"total_profit", "income_tax", "netprofit", "parent_netprofit"} <= set(
        income.lines
    )
    _, cash = resolved("cash_flow")
    assert {"begin_cce", "end_cce", "cce_add", "rate_change_effect"} <= set(cash.lines)


def test_a_row_that_is_not_in_the_catalog_is_kept_and_listed():
    body = [*BALANCE, row("其中：数据资源", "12.00", "10.00")]
    got = extracted(balance=body)
    statement, found = resolved("balance_sheet", got)
    assert found.unmatched == ("数据资源",)
    kept = next(r for r in statement.rows if r.label == "其中：数据资源")
    assert kept.current == Decimal("12.00")


def test_a_field_that_matches_two_rows_is_not_used():
    body = [*BALANCE, row("资产总计", "1.00", "1.00")]
    _, found = resolved("balance_sheet", extracted(balance=body))
    assert found.ambiguous == ("total_assets",)
    assert "total_assets" not in found.lines


def test_a_label_wrapped_onto_two_lines_is_put_back_together():
    body = [
        *INCOME[:4],
        row("归属于母公司股东的"),
        row("净利润", "700.00", "560.00"),
        row("2.少数股东损益", "50.00", "40.00"),
    ]
    _, found = resolved("income_statement", extracted(income=body))
    line_ = found.lines["parent_netprofit"]
    assert line_.row.current == Decimal("700.00") and line_.label_lines == 2


def test_a_wrapped_line_is_lent_to_one_row_only():
    body = [
        row("经营活动产生的现金流量净额", "900.00", "700.00"),
        row("投资活动产生的现金流量净额", "-600.00", "-500.00"),
        row("筹资活动产生的现金流量净额", "(100.00)", "(50.00)"),
        row("四、汇率变动对现金及现金", "10.00", "5.00"),
        row("等价物的影响"),
        row("净增加额", "210.00", "155.00"),
        row("加：期初现金及现金等价物余额", "1,000.00", "845.00"),
        row("六、期末现金及现金等价物余额", "1,210.00", "1,000.00"),
    ]
    _, found = resolved("cash_flow", extracted(cash=body))
    assert found.lines["rate_change_effect"].label_lines == 2
    assert "cce_add" not in found.lines and found.unmatched == ("净增加额",)


# ------------------------------------------------------------------ 勾稽


def test_a_consistent_report_balances_in_both_columns():
    for name, expected in (
        ("balance_sheet", 10),
        ("income_statement", 4),
        ("cash_flow", 4),
    ):
        statement, found = resolved(name)
        assert reconcile(statement, found) == (expected, ())


def test_a_wrong_number_is_caught_with_the_rule_and_the_column():
    body = list(BALANCE)
    body[7] = row("负债合计", "3,000.00", "3,100.00")
    statement, found = resolved("balance_sheet", extracted(balance=body))
    checked, broken = reconcile(statement, found)
    assert checked == 10
    assert [(b.rule_id, b.column, b.residual) for b in broken] == [
        ("R01", "prior", Decimal("-100.00")),
        ("R05", "prior", Decimal("100.00")),
    ]


def test_the_tolerance_is_one_unit_of_the_report():
    body = list(BALANCE)
    body[4] = row("资产总计", "10,001.00", "9,000.00")
    body[11] = row("负债和所有者权益总计", "10,001.00", "9,000.00")
    statement, found = resolved(
        "balance_sheet", extracted(balance=body, unit="单位：千元")
    )
    assert reconcile(statement, found)[1] == ()
    body[4] = row("资产总计", "10,001.01", "9,000.00")
    statement, found = resolved(
        "balance_sheet", extracted(balance=body, unit="单位：千元")
    )
    assert [b.rule_id for b in reconcile(statement, found)[1]] == ["R01", "R04"]


def test_a_rule_whose_fields_are_missing_is_not_counted():
    statement, found = resolved("balance_sheet", extracted(balance=BALANCE[:5]))
    assert reconcile(statement, found) == (
        2,
        (),
    )  # 只有「流动 + 非流动 = 资产总计」两列


def test_costs_printed_as_negatives_are_pointed_out_not_corrected():
    body = list(INCOME)
    body[2] = row("减：所得税费用", "(250.00)", "(200.00)")
    statement, found = resolved("income_statement", extracted(income=body))
    checked, broken = reconcile(statement, found)
    assert checked == 4
    assert [(b.rule_id, b.column, b.balanced_if_flipped) for b in broken] == [
        ("R07", "current", ("income_tax",)),
        ("R07", "prior", ("income_tax",)),
    ]
    assert values_of(found, "current")["income_tax"] == Decimal("-250.00")


# ------------------------------------------------------------------ 量级


@pytest.mark.parametrize(
    ("ours", "reference", "verdict"),
    [
        ("10000", "10000", "same"),
        ("10000", "9000", "same"),
        ("10000", "22000", "same"),  # 重述、合并范围变化
        ("10000", "1001", "same"),
        ("10000000", "10000", "unit_mismatch"),
        ("10000", "10400000", "unit_mismatch"),
        ("100000000", "10000", "unit_mismatch"),
        ("10000000000", "9800", "unit_mismatch"),
        ("1000000", "10000", "unit_mismatch"),
        ("10000", "300000", "different"),
        ("300000", "10000", "different"),
        ("10000", "-10000", "same"),
        ("-10000000", "10000", "unit_mismatch"),
        ("0", "0", "same"),
        ("0", "5", "different"),
        ("5", "0", "different"),
    ],
)
def test_magnitude_of_one_number(ours, reference, verdict):
    assert magnitude(Decimal(ours), Decimal(reference)) == verdict


def test_a_misread_unit_is_caught_by_the_reference():
    """STMT-08：年报以千元列示而认成了元，勾稽照样平，靠参照数发现。"""
    statement, found = resolved("balance_sheet")
    assert reconcile(statement, found)[1] == ()
    reference = {
        "total_assets": Decimal("10000000"),
        "total_liabilities": Decimal("3050000"),
        "total_equity": Decimal("6950000"),
    }
    verdict, each = check_unit(statement, found, reference)
    assert verdict == "unit_mismatch"
    assert set(each.values()) == {"unit_mismatch"}
    right = ExtractedStatement(
        "balance_sheet", 80, "千元", UNIT_ON_STATEMENT, False, statement.rows, {}
    )
    assert check_unit(right, found, reference)[0] == "confirmed"


def test_unit_check_without_anything_to_compare_says_so():
    statement, found = resolved("balance_sheet")
    assert check_unit(statement, found, {}) == ("no_reference", {})
    assert check_unit(statement, found, {"goodwill": Decimal(5)})[0] == "no_reference"


def test_unit_check_with_mixed_evidence_does_not_conclude():
    statement, found = resolved("balance_sheet")
    reference = {
        "total_assets": Decimal("10000"),
        "total_liabilities": Decimal("3000000"),
    }
    verdict, each = check_unit(statement, found, reference)
    assert verdict == "inconsistent"
    assert each == {"total_assets": "same", "total_liabilities": "unit_mismatch"}
    reference = {"total_assets": Decimal("10000"), "total_equity": Decimal("200")}
    verdict, each = check_unit(statement, found, reference)
    assert verdict == "inconsistent"
    assert each == {"total_assets": "same", "total_equity": "different"}


def test_the_prior_column_can_be_checked_too():
    statement, found = resolved("balance_sheet")
    assert check_unit(statement, found, {"total_assets": Decimal(9000)}, "prior") == (
        "confirmed",
        {"total_assets": "same"},
    )
    with pytest.raises(ValueError, match="unknown column"):
        values_of(found, "next")


# ------------------------------------------------------------------ 从 PDF 取词


def test_words_are_read_page_by_page_with_their_positions():
    pdf = (ONE_PAGE / "annual_2025_page6.pdf").read_bytes()
    pages = list(PdfPlumberWordsReader().read_words(pdf, first_page=1))
    assert [p.page_number for p in pages] == [1]
    words = pages[0].words
    assert len(words) > 100
    assert any("主要会计数据" in w.text for w in words)
    amounts = [w for w in words if is_number(w.text) and "," in w.text]
    assert len(amounts) > 20
    assert all(w.x0 < w.x1 and w.top > 0 for w in words)
    assert list(PdfPlumberWordsReader().read_words(pdf)) == []  # 默认从第 12 页起


@pytest.mark.parametrize("junk", [b"", b"not a pdf", b"%PDF-1.4 broken", b"PK\x03\x04"])
def test_what_is_not_a_readable_pdf_is_refused(junk):
    with pytest.raises(DatasetBuildError, match="report_unreadable"):
        list(PdfPlumberWordsReader().read_words(junk, first_page=1))


def test_the_size_limit_and_the_first_page_are_checked():
    pdf = (ONE_PAGE / "annual_2025_page6.pdf").read_bytes()
    with pytest.raises(DatasetBuildError, match="report_unreadable"):
        list(
            PdfPlumberWordsReader(max_bytes=len(pdf) - 1).read_words(pdf, first_page=1)
        )
    with pytest.raises(ValueError, match="first_page"):
        list(PdfPlumberWordsReader().read_words(pdf, first_page=0))
    with pytest.raises(ValueError, match="max_bytes"):
        PdfPlumberWordsReader(max_bytes=0)
