"""从年度报告的「主要会计数据」表里取关键数字（F-INFO-05）。

输入是已经抽好的表格与文字（抽取由基础设施做）。版式认不出来就说认不出来，不猜。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from app.domain.securities.dataset import OfficialFigure, ParsedReport, ReportPage
from app.domain.securities.financial_catalog import (
    BASIS_ORIGINAL,
    BASIS_RESTATED,
    KEY_ITEMS,
)

BASIS_COMPARATIVE = "比较数"
_TABLE_TITLE = "主要会计数据"
_YEAR = re.compile(r"^(20\d{2}|19\d{2})年末?$")
_NUMBER = re.compile(r"^-?\d{1,3}(,\d{3})*(\.\d+)?$|^-?\d+(\.\d+)?$")
_UNIT = re.compile(r"单位[:：]\s*(百万元|万元|千元|元)")
_FACTOR = {"元": 1.0, "千元": 1e3, "万元": 1e4, "百万元": 1e6}
_EXPLAIN_TITLE = "主要会计数据和财务指标的说明"
_NEXT_SECTION = re.compile(
    r"^\s*([（(][一二三四五六七八九十]+[)）]|[一二三四五六七八九十]+、|\d+\s*/\s*\d+)"
)
_LABELS = {item.label: item.item for item in KEY_ITEMS}
_MAX_EXPLANATION = 600


def _clean(cell: str | None) -> str:
    return re.sub(r"\s+", "", cell or "")


def parse_key_figures(
    pages: Sequence[ReportPage],
    *,
    report_fiscal_year: int,
    title: str,
    disclosed_date: str,
    revised: bool,
) -> ParsedReport:
    for index, page in enumerate(pages):
        for table in page.tables:
            if not table or not table[0] or _clean(table[0][0]) != _TABLE_TITLE:
                continue
            unit = _UNIT.search(page.text)
            if unit is None:
                return ParsedReport((), None, page.page_number, "unit_not_stated")
            figures = _figures(
                table,
                factor=_FACTOR[unit.group(1)],
                report_fiscal_year=report_fiscal_year,
                title=title,
                disclosed_date=disclosed_date,
                revised=revised,
                page=page.page_number,
            )
            if not figures:
                return ParsedReport((), None, page.page_number, "layout_not_recognized")
            following = pages[index + 1].text if index + 1 < len(pages) else ""
            return ParsedReport(
                tuple(figures),
                _explanation(page.text + "\n" + following),
                page.page_number,
            )
    return ParsedReport((), None, None, "table_not_found")


def _columns(header: Sequence[str]) -> tuple[list[int | None], list[bool]]:
    """表头 → 每一列是哪一年，以及这一列是不是靠「延续上一列」得到年份的。

    空表头是上一列的延续（合并单元格）。延续来的列只有在下一行标了「调整前 / 调整后」
    时才算数，由调用的一方判断。
    """
    years: list[int | None] = [None]
    continued: list[bool] = [False]
    previous: int | None = None
    for cell in header[1:]:
        text = _clean(cell)
        matched = _YEAR.match(text)
        if matched:
            previous = int(matched.group(1))
            years.append(previous)
            continued.append(False)
        elif text == "":
            years.append(previous)
            continued.append(previous is not None)
        else:
            previous = None  # 「增减」一类的列，后面的空表头不再延续年份
            years.append(None)
            continued.append(False)
    return years, continued


def _figures(
    table, *, factor, report_fiscal_year, title, disclosed_date, revised, page
) -> list[OfficialFigure]:
    figures: list[OfficialFigure] = []
    years: list[int | None] = []
    adjust: list[str] = []
    rows = list(table)
    position = 0
    while position < len(rows):
        row = [c or "" for c in rows[position]]
        position += 1
        label = _clean(row[0])
        cells = [_clean(c) for c in row]
        is_header = (label in ("", _TABLE_TITLE)) and any(
            _YEAR.match(c) for c in cells[1:]
        )
        if is_header:
            years, continued = _columns(row)
            adjust = [""] * len(row)
            if position < len(rows):
                following = [_clean(c) for c in rows[position]]
                if following[0] == "" and any(
                    c in ("调整后", "调整前") for c in following[1:]
                ):
                    adjust = [c if c in ("调整后", "调整前") else "" for c in following]
                    position += 1
            # 没有「调整前 / 调整后」标记的空表头列不是某一年的数。恒瑞医药 2023 年年报里，
            # 「本期比上年同期增减(%)」的表头被拆到了隔壁，百分比落在一个空表头的列里
            years = [
                None if continued[i] and not (i < len(adjust) and adjust[i]) else year
                for i, year in enumerate(years)
            ]
            continue
        item = _LABELS.get(label)
        if item is None or not years:
            continue
        for column, year in enumerate(years):
            if year is None or column >= len(cells):
                continue
            value = _number(cells[column])
            if value is None:
                continue
            marker = adjust[column] if column < len(adjust) else ""
            if year == report_fiscal_year and not marker:
                basis = BASIS_ORIGINAL
            elif marker == "调整后":
                basis = BASIS_RESTATED
            elif marker == "调整前":
                basis = BASIS_ORIGINAL
            elif year < report_fiscal_year:
                basis = BASIS_COMPARATIVE
            else:
                continue  # 年份比报告期还晚：版式不对，这一列不要
            figures.append(
                OfficialFigure(
                    fiscal_year=year,
                    item=item,
                    value=round(value * factor, 2),
                    basis=basis,
                    column_label=f"{year}年{(' ' + marker) if marker else ''}",
                    source_report=title,
                    report_fiscal_year=report_fiscal_year,
                    disclosed_date=disclosed_date,
                    page=page,
                    revised_report=revised,
                )
            )
    own = {f.item for f in figures if f.fiscal_year == report_fiscal_year}
    return figures if len(own) >= 3 else []  # 本期列认不出几个科目，就算没认出来


def _number(text: str) -> float | None:
    if not _NUMBER.match(text):
        return None
    return float(text.replace(",", ""))


def _explanation(text: str) -> str | None:
    """年报对「前三年主要会计数据」的说明，勾了「适用」才摘录，原样不改写。"""
    start = text.find(_EXPLAIN_TITLE)
    if start < 0:
        return None
    lines = text[start:].split("\n")[1:]
    if not lines or "√适用" not in _clean(lines[0]):
        return None
    kept: list[str] = []
    for line in lines[1:]:
        if _NEXT_SECTION.match(line) or len("".join(kept)) > _MAX_EXPLANATION:
            break
        if line.strip():
            kept.append(line.strip())
    body = "".join(kept)[:_MAX_EXPLANATION]
    return body or None
