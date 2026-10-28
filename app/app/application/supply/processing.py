"""加工器（SDD 0045 第 3.4 节）：按页文字、财报抽表（领域插件）。

- 按页文字：可抽文字的 PDF 按页切，带页码；扫描版的由 knowbase 建检索索引时从 RAGFlow 取回。
- 财报抽表：复用三张合并报表的抽取（`securities/report_extraction`，在 17 种版式的
  真实年报上试过；10-28 起半年报、一季报、三季报也抽），抽出来的每行进 knowbase 的原样层
  （科目名原文、单位、页码），认出科目的进标准化层（对到 knowbase 科目表的编码、换算到元）。
  认不出的不硬套；扫描版、三栏这类不敢用的照实写原因，不给数。

各类定期报告的两列：资产负债表是报告期末与上年年末；利润表、现金流量表是本期与上年同期，
本期的时长年报是一年、半年报半年、一季报一个季度、三季报年初到报告期末（九个月）。2021 年
以前沪市三季报的利润表把本季和年初至今印成四列，抽取照旧拒绝（照实写原因）。

年报、半年报另从附注的「现金流量表补充资料」抽折旧与摊销四项（`supplement`，为以后 DCF 这类
估值备数）；季报没有这一节。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from app.application.securities.report_extraction import extract_statements, resolve
from app.application.securities.report_extraction.supplement import (
    Supplement,
    extract_supplement,
)
from app.domain.securities.report_statements import (
    UNIT_FACTORS,
    UNIT_ON_STATEMENT,
    ExtractedStatement,
    PageWords,
)
from app.domain.supply.models import DescriptionKey

PAGES_VERSION = "pages-pdfplumber-1"
FACTS_VERSION = "periodic-statements-1"
RULE = "knowfeed-periodic-statements-1:catalog-label"

# 旧目录的字段 → knowbase 科目表（第 1 版）的编码；锁测试核这些编码在 knowbase 的科目表里
FIELD_TO_CODE: dict[str, str] = {
    "total_assets": "BS.TOTAL_ASSETS",
    "total_liabilities": "BS.TOTAL_LIABILITIES",
    "total_equity": "BS.TOTAL_EQUITY",
    "total_parent_equity": "BS.EQUITY_PARENT",
    "minority_equity": "BS.MINORITY_EQUITY",
    "total_liab_equity": "BS.TOTAL_LIABILITIES_EQUITY",
    "monetaryfunds": "BS.CASH",
    "accounts_rece": "BS.ACCOUNTS_RECEIVABLE",
    "inventory": "BS.INVENTORIES",
    "total_current_assets": "BS.TOTAL_CURRENT_ASSETS",
    "long_equity_invest": "BS.LT_EQUITY_INVEST",
    "fixed_asset": "BS.FIXED_ASSETS",
    "cip": "BS.CIP",
    "useright_asset": "BS.RIGHT_OF_USE",
    "intangible_asset": "BS.INTANGIBLES",
    "total_noncurrent_assets": "BS.TOTAL_NONCURRENT_ASSETS",
    "short_loan": "BS.SHORT_BORROWINGS",
    "accounts_payable": "BS.ACCOUNTS_PAYABLE",
    "noncurrent_liab_1year": "BS.NONCURRENT_LIAB_DUE_1Y",
    "total_current_liab": "BS.TOTAL_CURRENT_LIABILITIES",
    "long_loan": "BS.LT_BORROWINGS",
    "bond_payable": "BS.BONDS_PAYABLE",
    "lease_liab": "BS.LEASE_LIABILITIES",
    "total_noncurrent_liab": "BS.TOTAL_NONCURRENT_LIABILITIES",
    "share_capital": "BS.PAID_IN_CAPITAL",
    "unassign_rpofit": "BS.RETAINED_EARNINGS",
    "total_operate_income": "IS.TOTAL_REVENUE",
    "operate_income": "IS.REVENUE",
    "total_operate_cost": "IS.TOTAL_COST",
    "operate_cost": "IS.COST",
    "sale_expense": "IS.SELLING",
    "manage_expense": "IS.ADMIN",
    "research_expense": "IS.RND",
    "finance_expense": "IS.FINANCE",
    "fe_interest_expense": "IS.INTEREST_EXPENSE",
    "invest_income": "IS.INVESTMENT_INCOME",
    "invest_joint_income": "IS.INVESTMENT_INCOME_JV",
    "operate_profit": "IS.OPERATING_PROFIT",
    "total_profit": "IS.PROFIT_BEFORE_TAX",
    "income_tax": "IS.INCOME_TAX",
    "netprofit": "IS.NET_PROFIT",
    "parent_netprofit": "IS.NET_PROFIT_PARENT",
    "minority_interest": "IS.MINORITY_PROFIT",
    "deduct_parent_netprofit": "IS.NET_PROFIT_PARENT_DEDUCTED",
    "basic_eps": "IS.EPS_BASIC",
    "sales_services": "CF.SALES_CASH",
    "total_operate_inflow": "CF.OPERATING_INFLOW",
    "total_operate_outflow": "CF.OPERATING_OUTFLOW",
    "netcash_operate": "CF.NET_OPERATING",
    "construct_long_asset": "CF.CAPEX",
    "netcash_invest": "CF.NET_INVESTING",
    "assign_dividend_porfit": "CF.DIVIDENDS_INTEREST_PAID",
    "netcash_finance": "CF.NET_FINANCING",
    "rate_change_effect": "CF.FX_EFFECT",
    "cce_add": "CF.NET_CHANGE_CASH",
    "begin_cce": "CF.BEGINNING_CASH",
    "end_cce": "CF.ENDING_CASH",
}
# 补充资料里认出的字段 → knowbase 科目表（第 2 版）的编码
SUPPLEMENT_TO_CODE: dict[str, str] = {
    "depreciation_fixed_assets": "CF.DEPRECIATION_FIXED_ASSETS",
    "depreciation_right_of_use": "CF.DEPRECIATION_RIGHT_OF_USE",
    "amortization_intangibles": "CF.AMORTIZATION_INTANGIBLES",
    "amortization_long_term_prepaid": "CF.AMORTIZATION_LT_PREPAID",
}
SUPPLEMENT_LABEL = "现金流量表补充资料"
_STATEMENT_LABELS = {
    "balance_sheet": "合并资产负债表",
    "income_statement": "合并利润表",
    "cash_flow": "合并现金流量表",
}


@dataclass(frozen=True)
class ReportShape:
    """一类定期报告怎么读：从第几页起找报表、本期的时长、读到文件末尾算不算表结束。"""

    first_page: int
    flow_duration: str
    last_closes: bool
    supplement: bool  # 附注里有没有「现金流量表补充资料」


# 季报只有十几页，报表从前几页就开始，而且常常只印合并报表、后面没有下一个标题
REPORT_SHAPES: dict[str, ReportShape] = {
    "annual_report": ReportShape(12, "year", False, True),
    "semiannual_report": ReportShape(12, "half", False, True),
    "q1_report": ReportShape(1, "quarter", True, False),
    "q3_report": ReportShape(1, "ytd", True, False),
}


class PdfPages(Protocol):
    def text_pages(self, pdf: bytes) -> list[tuple[int, str]]: ...


class PdfWords(Protocol):
    def read_words(
        self, pdf: bytes, *, first_page: int = ...
    ) -> Iterable[PageWords]: ...


def pages_reading(pdf: bytes, reader: PdfPages) -> list[dict[str, object]]:
    """按页文字：[{page, text}]；一页字都没有的也列出（扫描版的页），文字为空。"""
    return [{"page": n, "text": text} for n, text in reader.text_pages(pdf)]


@dataclass
class StatementFacts:
    raw: list[dict[str, object]] = field(default_factory=list)
    standard: list[dict[str, object]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def report_statements(
    pdf: bytes, key: DescriptionKey, reader: PdfWords
) -> StatementFacts:
    """从定期报告抽三张合并报表（年报、半年报、一季报、三季报）。"""
    shape = REPORT_SHAPES[key.doc_type]
    return statements_from_pages(
        reader.read_words(pdf, first_page=shape.first_page), key
    )


def statements_from_pages(
    pages: Iterable[PageWords], key: DescriptionKey
) -> StatementFacts:
    assert key.period_end is not None
    shape = REPORT_SHAPES[key.doc_type]
    current_end = key.period_end
    # 同一串页：报表抽完接着往后找补充资料（附注在报表后面），不再从头读一遍
    remaining = iter(pages)
    extraction = extract_statements(remaining, last_closes=shape.last_closes)
    out = StatementFacts(
        problems=[
            f"{p.code}:{p.statement or ''}:{p.page or ''}" for p in extraction.problems
        ]
    )
    for name, statement in extraction.statements.items():
        if name == "balance_sheet":
            # 资产负债表的上期列是上年年末（年报里就是上年同一天）
            duration = "instant"
            prior_end = date(current_end.year - 1, 12, 31)
        else:
            duration = shape.flow_duration
            prior_end = _year_before(current_end)
        _one(out, name, statement, current_end, prior_end, duration)
    if shape.supplement and "cash_flow" in extraction.statements:
        supplement = extract_supplement(remaining)
        out.problems.extend(supplement.problems)
        cash_flow = extraction.statements["cash_flow"]
        # 补充资料没写单位就随现金流量表；那张表的单位也不是报表页写明的，只给原样层
        trusted = (
            supplement.unit is not None or cash_flow.unit_source == UNIT_ON_STATEMENT
        )
        if supplement.found and not trusted:
            out.problems.append("supplement_unit_not_on_statement")
        _supplement(
            out,
            supplement,
            supplement.unit or cash_flow.unit,
            trusted,
            current_end,
            _year_before(current_end),
            shape.flow_duration,
        )
    return out


def _supplement(
    out: StatementFacts,
    supplement: Supplement,
    unit: str,
    standardize: bool,
    current_end: date,
    prior_end: date,
    duration: str,
) -> None:
    """补充资料认出的几行：原样层一行一个数，标准化层换算到元（单位没写就随现金流量表）。"""
    factor = UNIT_FACTORS[unit]
    for n, (name, row) in enumerate(supplement.found.items()):
        for period_end, value in ((current_end, row.current), (prior_end, row.prior)):
            if value is None:
                continue
            out.raw.append(
                {
                    "label": row.label[:200],
                    "value": str(value),
                    "unit": unit,
                    "period_end": period_end.isoformat(),
                    "duration": duration,
                    "consolidation": "consolidated",
                    "statement_label": SUPPLEMENT_LABEL,
                    "page": row.page,
                    "row_no": n + 1,
                }
            )
            if not standardize:
                continue
            out.standard.append(
                {
                    "code": SUPPLEMENT_TO_CODE[name],
                    "value": str(value * factor),
                    "period_end": period_end.isoformat(),
                    "duration": duration,
                    "consolidation": "consolidated",
                    "derived_from": [len(out.raw) - 1],
                    "rule": RULE,
                    "page": row.page,
                }
            )


def _one(
    out: StatementFacts,
    name: str,
    statement: ExtractedStatement,
    current_end: date,
    prior_end: date,
    duration: str,
) -> None:
    label = _STATEMENT_LABELS[name]
    # 每一行原样进原样层（本期一行、上期一行）；记下每个下标，标准化层引用它
    index: dict[tuple[int, str], int] = {}
    for n, row in enumerate(statement.rows):
        for column, period_end, value in (
            ("current", current_end, row.current),
            ("prior", prior_end, row.prior),
        ):
            if value is None:
                continue
            index[(n, column)] = len(out.raw)
            out.raw.append(
                {
                    "label": row.label[:200],
                    "value": str(value),
                    "unit": statement.unit,
                    "period_end": period_end.isoformat(),
                    "duration": duration,
                    "consolidation": "consolidated",
                    "statement_label": label,
                    "page": row.page,
                    "row_no": n + 1,
                }
            )
    if statement.unit_source != UNIT_ON_STATEMENT:
        # 单位不是报表页上写明的：原样照给，标准化（换算到元）要参照数核过才敢做
        out.problems.append(f"unit_not_on_statement:{name}:{statement.unit_source}")
        return
    resolution = resolve(name, statement.rows)
    rows = list(statement.rows)
    for field_name, line in resolution.lines.items():
        code = FIELD_TO_CODE.get(field_name)
        if code is None:
            continue
        # 按同一个对象找下标：两行科目名、数都一样时按值找会找错
        n = next(i for i, r in enumerate(rows) if r is line.row)
        for column, period_end, value in (
            ("current", current_end, line.row.current),
            ("prior", prior_end, line.row.prior),
        ):
            if value is None or (n, column) not in index:
                continue
            out.standard.append(
                {
                    "code": code,
                    "value": str(value * statement.factor),
                    "period_end": period_end.isoformat(),
                    "duration": duration,
                    "consolidation": "consolidated",
                    "derived_from": [index[(n, column)]],
                    "rule": RULE,
                    "page": line.row.page,
                }
            )
    for ambiguous in resolution.ambiguous:
        out.problems.append(f"ambiguous:{name}:{ambiguous}")


def _year_before(day: date) -> date:
    try:
        return day.replace(year=day.year - 1)
    except ValueError:
        return day.replace(year=day.year - 1, day=28)


def wanted_readings(requested: Iterable[str], done: set[str]) -> str:
    """需求项该是什么状态：要的都齐了就是已就绪；只要原件的，不因加工失败降级。"""
    want = set(requested)
    if want <= done:
        return "ready"
    if "original" in done:
        return "partial"
    return "failed"
