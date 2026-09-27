"""口径判定（F-INFO-05）：报表里某年的数，是当年披露的，还是后来追溯调整过的。

判定只靠与法定披露原文的比对：
- 与该年年报「本期」列一致 → 原始披露；
- 与后一年年报里标了「调整后」的列一致，或与后面年报的比较数一致而与当年披露不一致
  → 追溯调整后；
- 都对不上，或没有可比的原文 → 未核实。
"""

from __future__ import annotations

from collections.abc import Sequence

from app.application.securities.dataset.official_report import BASIS_COMPARATIVE
from app.domain.securities.dataset import OfficialFigure, StatementRow
from app.domain.securities.financial_catalog import (
    BASIS_ORIGINAL,
    BASIS_RESTATED,
    BASIS_UNVERIFIED,
    KEY_ITEMS,
    TOLERANCE_YUAN,
)

_MIN_ITEMS = 3
_ANNUAL = "年报"


def _values(rows_by_statement, year: int) -> dict[str, float]:
    found: dict[str, float] = {}
    for item in KEY_ITEMS:
        for row in rows_by_statement.get(item.table, ()):
            if row.report_type == _ANNUAL and row.fiscal_year == year:
                value = row.values.get(item.field)
                if value is not None:
                    found[item.item] = value
    return found


def _agrees(statement: dict[str, float], official: dict[str, float]) -> bool | None:
    """全部可比科目都一致才算一致；可比科目不足时返回 None（不能判）。"""
    common = [k for k in official if k in statement]
    if len(common) < _MIN_ITEMS:
        return None
    return all(abs(statement[k] - official[k]) <= TOLERANCE_YUAN for k in common)


def assign_basis(
    rows_by_statement: dict[str, list[StatementRow]],
    figures: Sequence[OfficialFigure],
) -> dict[int, tuple[str, str | None]]:
    """给年报行标口径，返回 {会计年度: (口径, 依据的年报)}。中报季报保持未核实。"""
    years = sorted(
        {
            row.fiscal_year
            for rows in rows_by_statement.values()
            for row in rows
            if row.report_type == _ANNUAL
        }
    )
    decided: dict[int, tuple[str, str | None]] = {}
    for year in years:
        decided[year] = _decide(year, _values(rows_by_statement, year), figures)
    for rows in rows_by_statement.values():
        for row in rows:
            if row.report_type != _ANNUAL:
                continue
            basis, against = decided[row.fiscal_year]
            row.basis = basis
            row.verified = basis != BASIS_UNVERIFIED
            row.verified_against = against
    return decided


def _decide(
    year: int, statement: dict[str, float], figures: Sequence[OfficialFigure]
) -> tuple[str, str | None]:
    def column(basis: str, *, own: bool | None) -> list[tuple[str, dict[str, float]]]:
        """按年报分组的一列。先披露的排前面；修订版排在原版后面。"""
        grouped: dict[tuple[str, bool, str], dict[str, float]] = {}
        for f in figures:
            if f.fiscal_year != year or f.basis != basis:
                continue
            if own is not None and (f.report_fiscal_year == year) != own:
                continue
            key = (f.disclosed_date, f.revised_report, f.source_report)
            grouped.setdefault(key, {})[f.item] = f.value
        return [(k[2], v) for k, v in sorted(grouped.items())]

    originals = column(BASIS_ORIGINAL, own=True) + column(BASIS_ORIGINAL, own=False)
    for report, values in originals:
        if _agrees(statement, values):
            return BASIS_ORIGINAL, report
    for report, values in column(BASIS_RESTATED, own=None):
        if _agrees(statement, values):
            return BASIS_RESTATED, report
    if originals:  # 当年披露过，而报表与它不一致，却与后面年报的比较数一致
        for report, values in column(BASIS_COMPARATIVE, own=None):
            if _agrees(statement, values):
                return BASIS_RESTATED, report
    return BASIS_UNVERIFIED, None
