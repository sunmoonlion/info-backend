"""加工器（SDD 0045 第 3.4 节）：按页文字、财报抽表（领域插件）。

- 按页文字：可抽文字的 PDF 按页切，带页码；扫描版的由 knowbase 建检索索引时从 RAGFlow 取回。
- 财报抽表：复用年报三张合并报表的抽取（`securities/report_extraction`，在 17 种版式的
  真实年报上试过），抽出来的每行进 knowbase 的原样层（科目名原文、单位、页码），认出科目
  的进标准化层（对到 knowbase 科目表的编码、换算到元）。认不出的不硬套；扫描版、三栏
  这类不敢用的照实写原因，不给数。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Protocol

from app.application.securities.report_extraction import extract_statements, resolve
from app.domain.securities.report_statements import (
    UNIT_ON_STATEMENT,
    ExtractedStatement,
    PageWords,
)
from app.domain.supply.models import DescriptionKey

PAGES_VERSION = "pages-pdfplumber-1"
FACTS_VERSION = "annual-statements-1"
RULE = "knowfeed-annual-statements-1:catalog-label"

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
_STATEMENT_LABELS = {
    "balance_sheet": "合并资产负债表",
    "income_statement": "合并利润表",
    "cash_flow": "合并现金流量表",
}
_DURATION = {
    "balance_sheet": "instant",
    "income_statement": "year",
    "cash_flow": "year",
}


class PdfPages(Protocol):
    def text_pages(self, pdf: bytes) -> list[tuple[int, str]]: ...


class PdfWords(Protocol):
    def read_words(self, pdf: bytes) -> Iterable[PageWords]: ...


def pages_reading(pdf: bytes, reader: PdfPages) -> list[dict[str, object]]:
    """按页文字：[{page, text}]；一页字都没有的也列出（扫描版的页），文字为空。"""
    return [{"page": n, "text": text} for n, text in reader.text_pages(pdf)]


@dataclass
class StatementFacts:
    raw: list[dict[str, object]] = field(default_factory=list)
    standard: list[dict[str, object]] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def annual_statements(
    pdf: bytes, key: DescriptionKey, reader: PdfWords
) -> StatementFacts:
    """从年报抽三张合并报表：本期列是这一报告期，上期列是上年同期。"""
    return statements_from_pages(reader.read_words(pdf), key)


def statements_from_pages(
    pages: Iterable[PageWords], key: DescriptionKey
) -> StatementFacts:
    assert key.period_end is not None
    current_end = key.period_end
    prior_end = _year_before(current_end)
    extraction = extract_statements(pages)
    out = StatementFacts(
        problems=[
            f"{p.code}:{p.statement or ''}:{p.page or ''}" for p in extraction.problems
        ]
    )
    for name, statement in extraction.statements.items():
        _one(out, name, statement, current_end, prior_end)
    return out


def _one(
    out: StatementFacts,
    name: str,
    statement: ExtractedStatement,
    current_end: date,
    prior_end: date,
) -> None:
    duration = _DURATION[name]
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
