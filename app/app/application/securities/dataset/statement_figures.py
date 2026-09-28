"""年报「主要会计数据」表认不出来时，从年报自己的三张合并报表里取关键数字。

「主要会计数据」的解析只认一种版式；深交所、北交所的年报，以及一部分上交所年报，那张表取不出来。
关键数字在合并报表里都有（扣除非经常性损益的净利润除外），所以改从合并报表取。

取出来的数只用来核对第三方数据的口径：当年年报的本期列是原始披露，上期列是比较数。
一张表只有在读出来没有问题、勾稽做过而且全平、单位可信的时候才用。
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from app.application.securities.dataset.official_report import BASIS_COMPARATIVE
from app.application.securities.report_extraction import (
    check_unit,
    reconcile,
    resolve,
    values_of,
)
from app.domain.securities.dataset import OfficialFigure, ParsedReport
from app.domain.securities.financial_catalog import BASIS_ORIGINAL, KEY_ITEMS
from app.domain.securities.report_statements import (
    UNIT_ON_STATEMENT,
    ReportExtraction,
)

MIN_ITEMS = 3
_TITLES = {
    "balance_sheet": "合并资产负债表",
    "income_statement": "合并利润表",
    "cash_flow": "合并现金流量表",
}
_COLUMNS = (
    ("current", 0, BASIS_ORIGINAL, "本期"),
    ("prior", -1, BASIS_COMPARATIVE, "上期"),
)


def figures_from_statements(
    extraction: ReportExtraction,
    *,
    report_fiscal_year: int,
    title: str,
    disclosed_date: str,
    revised: bool,
    reference_in_yuan: Mapping[str, Mapping[str, Decimal]],
) -> ParsedReport:
    """reference_in_yuan：表名 → {字段: 以元计的参照数}，只在单位不是报表自己写明时用来核对量级。"""
    figures: list[OfficialFigure] = []
    first_page: int | None = None
    refused: list[str] = []
    for name in _TITLES:
        statement = extraction.statements.get(name)
        if statement is None:
            refused.append(f"{name}:not_extracted")
            continue
        lines = resolve(name, statement.rows)
        checked, broken = reconcile(statement, lines)
        if checked == 0:
            refused.append(f"{name}:not_reconciled")
            continue
        if broken:
            refused.append(f"{name}:unbalanced")
            continue
        if statement.unit_source != UNIT_ON_STATEMENT:
            verdict, _ = check_unit(statement, lines, reference_in_yuan.get(name, {}))
            if verdict != "confirmed":
                refused.append(f"{name}:unit_{verdict}")
                continue
        first_page = min(first_page or statement.start_page, statement.start_page)
        for column, offset, basis, label in _COLUMNS:
            values = values_of(lines, column)
            for item in KEY_ITEMS:
                if item.table != name or item.field not in values:
                    continue
                figures.append(
                    OfficialFigure(
                        fiscal_year=report_fiscal_year + offset,
                        item=item.item,
                        value=float(values[item.field] * statement.factor),
                        basis=basis,
                        column_label=f"{_TITLES[name]} {label}",
                        source_report=title,
                        report_fiscal_year=report_fiscal_year,
                        disclosed_date=disclosed_date,
                        page=lines.lines[item.field].row.page,
                        revised_report=revised,
                        precision=float(statement.factor),
                    )
                )
    current = {f.item for f in figures if f.fiscal_year == report_fiscal_year}
    if len(current) < MIN_ITEMS:
        reasons = "；".join(refused) or "too_few_items"
        return ParsedReport((), None, None, f"statements_unusable（{reasons}）")
    return ParsedReport(tuple(figures), None, first_page)
