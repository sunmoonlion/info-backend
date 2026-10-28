"""半年报、一季报、三季报的抽表（10-28，SDD 0045 块 6）。

夹具是从巨潮的定期报告里裁出来的词和坐标（只留三张合并报表所在的页，季报留到文件末尾），
文字一个没改；见 fixtures/securities/report_words/README.md。期望值里的营业收入、归母
净利润、经营现金流在 10-28 与同一份报告前几页「主要会计数据」逐项核过。
"""

from __future__ import annotations

import gzip
import json
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from app.application.securities.report_extraction import extract_statements
from app.application.securities.report_extraction.layout import (
    OTHER,
    statement_of,
    title_of,
)
from app.application.securities.report_extraction.supplement import (
    extract_supplement,
    field_of,
)
from app.application.supply.processing import REPORT_SHAPES, statements_from_pages
from app.domain.securities.report_statements import PageWords, Word
from app.domain.supply.models import DescriptionKey

FIXTURES = Path(__file__).parent / "fixtures" / "securities" / "report_words"

# 名称 → （期末, 时长, {编码: (本期, 上期)}）。资产负债表的上期是上年年末，利润表、
# 现金流量表的上期是上年同期
EXPECTED: dict[str, tuple[str, dict[str, tuple[str, str]]]] = {
    "600519-semiannual_report-2025-06-30": (
        "half",
        {
            "BS.TOTAL_ASSETS": ("292257789095.51", "298944579918.70"),
            "IS.TOTAL_REVENUE": ("91093762553.97", "83451164646.53"),
            "IS.NET_PROFIT_PARENT": ("45402962298.10", "41695610983.37"),
            "CF.NET_OPERATING": ("13119061031.33", "36621833812.63"),
        },
    ),
    # 深交所，单位千元
    "300750-semiannual_report-2025-06-30": (
        "half",
        {
            "BS.TOTAL_ASSETS": ("867181431000", "786658123000"),
            "IS.TOTAL_REVENUE": ("178886253000", "166766834000"),
            "IS.NET_PROFIT_PARENT": ("30485139000", "22864987000"),
            "CF.NET_OPERATING": ("58687066000", "44708955000"),
        },
    ),
    "600009-q1_report-2025-03-31": (
        "quarter",
        {
            "BS.TOTAL_ASSETS": ("68742488915.32", "70046212107.27"),
            "IS.TOTAL_REVENUE": ("3172262268.56", "3029379846.52"),
            "IS.NET_PROFIT_PARENT": ("518983551.43", "385764035.91"),
            "CF.NET_OPERATING": ("704585525.09", "490141672.53"),
        },
    ),
    # 深交所新版：「合并年初到报告期末利润表」，只印合并报表，现金流量表读到文件末尾
    "000858-q3_report-2025-09-30": (
        "ytd",
        {
            "BS.TOTAL_ASSETS": ("191181308049.92", "188252218704.17"),
            "IS.TOTAL_REVENUE": ("30637727654.97", "67915580112.25"),
            "IS.NET_PROFIT_PARENT": ("6474551792.18", "24930782556.55"),
            "CF.NET_OPERATING": ("28247488416.94", "29794118733.59"),
        },
    ),
    # 科创板，单位千元，只印合并报表
    "688981-q3_report-2025-09-30": (
        "ytd",
        {
            "BS.TOTAL_ASSETS": ("351368416000", "353415296000"),
            "IS.TOTAL_REVENUE": ("49510416000", "41878720000"),
            "IS.NET_PROFIT_PARENT": ("3818071000", "2706192000"),
            "CF.NET_OPERATING": ("12287551000", "12264180000"),
        },
    ),
    # 「负债和所有者权益（或股东权益）」的「总计」折到了下一页
    "600519-q3_report-2025-09-30": (
        "ytd",
        {
            "BS.TOTAL_LIABILITIES_EQUITY": ("304738184929.86", "298944579918.70"),
            "IS.TOTAL_REVENUE": ("130903889634.88", "123122542625.45"),
            "IS.NET_PROFIT_PARENT": ("64626746712.18", "60827552118.51"),
            "CF.NET_OPERATING": ("38196802155.27", "44421386217.45"),
        },
    ),
    # 深交所旧版：另有一张「合并本报告期利润表」不要；「母公司年初到报告期末现金流量表」15 个字
    "000858-q3_report-2019-09-30": (
        "ytd",
        {
            "BS.TOTAL_ASSETS": ("92757067719.93", "86094265733.09"),
            "IS.TOTAL_REVENUE": ("37101632092.99", "29250287403.09"),
            "IS.NET_PROFIT_PARENT": ("12543547315.61", "9494416489.82"),
            "CF.NET_OPERATING": ("16191322654.60", "3876870034.45"),
        },
    ),
}


def fixture(name: str) -> tuple[dict, list[PageWords]]:
    body = json.loads(gzip.decompress((FIXTURES / f"{name}.json.gz").read_bytes()))
    pages = [
        PageWords(p["page"], tuple(Word(*w) for w in p["words"])) for p in body["pages"]
    ]
    return body["source"], pages


def key_of(source: dict) -> DescriptionKey:
    code = source["security_code"]
    return DescriptionKey(
        f"{code}.{'SH' if code.startswith('6') else 'SZ'}",
        source["doc_type"],
        None,
        date.fromisoformat(source["period_end"]),
    )


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_a_periodic_report_gives_its_periods_and_durations(name):
    source, pages = fixture(name)
    key = key_of(source)
    assert key.period_end is not None
    facts = statements_from_pages(pages, key)
    # 半年报的夹具只裁了报表页，附注里的补充资料不在里面
    supplement = REPORT_SHAPES[key.doc_type].supplement
    assert facts.problems == (["supplement_not_found"] if supplement else [])
    flow, values = EXPECTED[name]
    end = key.period_end
    year_end = date(end.year - 1, 12, 31)
    same_day = date(end.year - 1, end.month, end.day)
    got = {
        (s["code"], s["period_end"], s["duration"]): Decimal(s["value"])
        for s in facts.standard
    }
    for code, (current, prior) in values.items():
        duration = "instant" if code.startswith("BS.") else flow
        before = year_end if code.startswith("BS.") else same_day
        assert got[(code, end.isoformat(), duration)] == Decimal(current), code
        assert got[(code, before.isoformat(), duration)] == Decimal(prior), code
    # 三张表都在，每个标准化的数都追得到原样层那一行
    assert {r["statement_label"] for r in facts.raw} == {
        "合并资产负债表",
        "合并利润表",
        "合并现金流量表",
    }
    for s in facts.standard:
        raw = facts.raw[s["derived_from"][0]]
        assert raw["page"] == s["page"] and raw["period_end"] == s["period_end"]


def test_an_old_shanghai_q3_income_statement_in_four_columns_is_refused():
    # 2021 年以前沪市三季报的利润表把本季与年初至今印成四列：照实拒绝，另两张照给
    source, pages = fixture("600009-q3_report-2020-09-30")
    facts = statements_from_pages(pages, key_of(source))
    assert facts.problems
    assert all(
        p.startswith("more_than_two_columns:income_statement") for p in facts.problems
    )
    labels = {r["statement_label"] for r in facts.raw}
    assert labels == {"合并资产负债表", "合并现金流量表"}


def test_a_quarterly_report_without_parent_statements_ends_at_the_last_page():
    _, pages = fixture("688981-q3_report-2025-09-30")
    # 不说读完了整份文件，最后一张表就不算结束（年报的规矩）
    assert "cash_flow" not in extract_statements(pages).statements
    assert "cash_flow" in extract_statements(pages, last_closes=True).statements


@pytest.mark.parametrize(
    ("line", "name"),
    [
        ("2、合并年初到报告期末利润表", "income_statement"),
        ("合并年初至报告期末利润表", "income_statement"),
        ("3、合并年初到报告期末现金流量表", "cash_flow"),
        ("合并年初至报告期末现金流量表", "cash_flow"),
    ],
)
def test_year_to_date_titles_are_the_statements(line, name):
    assert statement_of(title_of(line)) == (name, False)


@pytest.mark.parametrize(
    "line",
    [
        "3、合并本报告期利润表",
        "4、母公司本报告期利润表",
        "6、母公司年初至报告期末利润表",
        "8、母公司年初到报告期末现金流量表",
    ],
)
def test_other_quarterly_titles_end_the_statement_before(line):
    title = title_of(line)
    assert statement_of(title) == (None, False)
    assert OTHER.match(title)


def test_every_report_type_has_a_shape():
    assert set(REPORT_SHAPES) == {
        "annual_report",
        "semiannual_report",
        "q1_report",
        "q3_report",
    }
    assert REPORT_SHAPES["q3_report"].flow_duration == "ytd"
    assert REPORT_SHAPES["q1_report"].first_page == 1


def test_periodic_fixtures_say_where_they_come_from():
    for name in [*EXPECTED, "600009-q3_report-2020-09-30"]:
        source, _ = fixture(name)
        assert source["source"] == "cninfo"
        assert source["copyright_status"] == "public_disclosure"
        assert name == (
            f"{source['security_code']}-{source['doc_type']}-{source['period_end']}"
        )
        assert len(source["pdf_sha256"]) == 64 and source["announcement_id"].isdecimal()


# —— 附注「现金流量表补充资料」里的折旧与摊销 ——

SUPPLEMENTS: dict[str, dict[str, tuple[str, str]]] = {
    # 「固定资产折旧、油气资产折耗、生／产性生物资产折旧」折成两行，数印在中间
    "600519-annual_report-2024-12-31": {
        "depreciation_fixed_assets": ("1721165327.14", "1651428992.20"),
        "depreciation_right_of_use": ("94492678.29", "72443839.21"),
        "amortization_intangibles": ("249170059.35", "196656866.73"),
        "amortization_long_term_prepaid": ("20191550.34", "16886608.86"),
    },
    # 只写「固定资产折旧」；补充资料下面不写单位，表头是「2024年 2023年」
    "002594-annual_report-2024-12-31": {
        "depreciation_fixed_assets": ("56920062", "37715404"),
        "depreciation_right_of_use": ("2643241", "1390498"),
        "amortization_intangibles": ("5514039", "4174175"),
        "amortization_long_term_prepaid": ("1827327", "270147"),
    },
    # 单位千元；「使用权资产摊销」；没有长期待摊费用摊销这一行的数
    "688981-annual_report-2024-12-31": {
        "depreciation_fixed_assets": ("22687973", "18260000"),
        "depreciation_right_of_use": ("227078", "344125"),
        "amortization_intangibles": ("240966", "255722"),
    },
    # 半年报；「……生产性生物资／产折旧」折行
    "300750-semiannual_report-2025-06-30": {
        "depreciation_fixed_assets": ("11355579", "11284923"),
        "depreciation_right_of_use": ("105656", "74913"),
        "amortization_intangibles": ("244874", "214967"),
        "amortization_long_term_prepaid": ("667604", "647959"),
    },
}


@pytest.mark.parametrize("name", sorted(SUPPLEMENTS))
def test_depreciation_and_amortization_come_from_the_supplement(name):
    source, pages = fixture(f"{name}-supplement")
    got = extract_supplement(iter(pages))
    assert got.problems == []
    assert {k: (str(v.current), str(v.prior)) for k, v in got.found.items()} == (
        SUPPLEMENTS[name]
    )
    assert source["source"] == "cninfo"
    assert source["copyright_status"] == "public_disclosure"


def test_the_supplement_rows_become_facts_with_the_report_s_periods():
    # 年报整份读：报表抽完接着往后找补充资料；单位千元换算到元
    _, statement_pages = fixture("300750-semiannual_report-2025-06-30")
    _, supplement_pages = fixture("300750-semiannual_report-2025-06-30-supplement")
    key = DescriptionKey("300750.SZ", "semiannual_report", None, date(2025, 6, 30))
    facts = statements_from_pages(statement_pages + supplement_pages, key)
    assert facts.problems == []
    got = {
        (s["code"], s["period_end"], s["duration"]): s["value"]
        for s in facts.standard
        if s["code"].startswith(("CF.DEPRECIATION", "CF.AMORTIZATION"))
    }
    assert got[("CF.DEPRECIATION_FIXED_ASSETS", "2025-06-30", "half")] == "11355579000"
    assert got[("CF.AMORTIZATION_LT_PREPAID", "2024-06-30", "half")] == "647959000"
    assert len(got) == 8
    raw = [r for r in facts.raw if r["statement_label"] == "现金流量表补充资料"]
    assert len(raw) == 8 and {r["unit"] for r in raw} == {"千元"}


def test_a_quarterly_report_does_not_look_for_a_supplement():
    _, pages = fixture("688981-q3_report-2025-09-30")
    key = DescriptionKey("688981.SH", "q3_report", None, date(2025, 9, 30))
    assert statements_from_pages(pages, key).problems == []


def test_a_report_without_the_supplement_says_so():
    assert extract_supplement(iter([])).problems == ["supplement_not_found"]


@pytest.mark.parametrize(
    ("label", "name"),
    [
        ("固定资产折旧、油气资产折耗、生产性生物资产折旧", "depreciation_fixed_assets"),
        ("固定资产折旧", "depreciation_fixed_assets"),
        ("固定资产折旧、油气资产折耗、生产性生物资产", "depreciation_fixed_assets"),
        ("使用权资产摊销", "depreciation_right_of_use"),
        ("无形资产摊销", "amortization_intangibles"),
        ("长期待摊费用摊销", "amortization_long_term_prepaid"),
        ("固定资产折旧、无形资产摊销", None),
        ("资产减值准备", None),
    ],
)
def test_supplement_labels(label, name):
    assert field_of(label) == name
