"""质量检查（F-INFO-06）。入库前跑；任何一条「拦住」的检查不通过，数据集不发布。

硬性拦截只针对近若干年（F-INFO-13，所有者 2026-09-28 定）。更早的报告期同期勾稽不平，
只剔除那一期并标注，不拦整个数据集：第三方网站的老数据里有少量对不上的地方
（2026-09-28 批量实测，出问题的多是十几二十年前或上市之前的数据），
不能为了它们把近些年的数据也拦掉。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date

from app.application.securities.dataset.official_report import BASIS_COMPARATIVE
from app.domain.securities.dataset import (
    DisclosureEntry,
    OfficialFigure,
    QualityCheck,
    QualityReport,
    StatementRow,
)
from app.domain.securities.financial_catalog import (
    BASIS_ORIGINAL,
    BASIS_RESTATED,
    BASIS_UNVERIFIED,
    KEY_ITEMS,
    RECONCILIATION_RULES,
    STATEMENT_FIELDS,
    TOLERANCE_YUAN,
)

_ANNUAL = "年报"
_MAX_LISTED = 20
_STALE_DAYS = 200
_REQUIRED = {
    "balance_sheet": ("total_assets", "total_liabilities", "total_equity"),
    "income_statement": ("operate_income", "netprofit", "parent_netprofit"),
    "cash_flow": ("netcash_operate", "cce_add", "end_cce"),
}


DEFAULT_HARD_YEARS = 10


@dataclass(frozen=True)
class Screening:
    """筛过之后的报表行，以及被剔除的报告期。"""

    rows: dict[str, list[StatementRow]]
    first_hard_year: int | None  # 这一年及以后的数据有问题就拦；None 表示没有任何数据
    excluded: tuple[dict, ...] = ()
    hard_years: int = DEFAULT_HARD_YEARS
    _dropped: frozenset[str] = field(default_factory=frozenset, repr=False)


def _residual(rule, row: StatementRow) -> float | None:
    total = 0.0
    for name, sign, optional in rule.terms:
        value = row.values.get(name)
        if value is None:
            if optional:
                continue
            return None
        total += sign * value
    return total


def screen(
    rows_by_statement: dict[str, list[StatementRow]],
    *,
    hard_years: int = DEFAULT_HARD_YEARS,
) -> Screening:
    """硬性拦截范围之前的报告期，同期勾稽不平的整期剔除（三张表一起）。

    范围之内的一行不动：它们不平要由后面的检查拦住，不能在这里悄悄去掉。
    """
    if hard_years < 1:
        raise ValueError("hard_years must be at least 1")
    years = [r.fiscal_year for rows in rows_by_statement.values() for r in rows]
    if not years:
        return Screening(dict(rows_by_statement), None, hard_years=hard_years)
    first = max(years) - hard_years + 1
    reasons: dict[str, list[dict]] = {}
    kinds: dict[str, str] = {}
    for rule in RECONCILIATION_RULES:
        for row in rows_by_statement.get(rule.source_table) or []:
            if row.fiscal_year >= first:
                continue
            residual = _residual(rule, row)
            if residual is not None and abs(residual) > TOLERANCE_YUAN:
                kinds[row.report_date] = row.report_type
                reasons.setdefault(row.report_date, []).append(
                    {"rule_id": rule.rule_id, "residual": round(residual, 2)}
                )
    dropped = frozenset(reasons)
    kept = {
        statement: [r for r in rows if r.report_date not in dropped]
        for statement, rows in rows_by_statement.items()
    }
    excluded = tuple(
        {
            "report_date": report_date,
            "report_type": kinds[report_date],
            "rules": reasons[report_date],
        }
        for report_date in sorted(reasons)
    )
    return Screening(kept, first, excluded, hard_years, dropped)


def run_checks(
    rows_by_statement: dict[str, list[StatementRow]],
    figures: Sequence[OfficialFigure],
    calendar: Sequence[DisclosureEntry],
    *,
    today: date,
    screening: Screening | None = None,
) -> QualityReport:
    """rows_by_statement 是筛过之后的行。不给 screening 就是全部期间都硬性拦截。"""
    window = sorted({f.report_fiscal_year for f in figures})
    first = screening.first_hard_year if screening else None
    checks = [
        _structure(rows_by_statement, window),
        *_reconciliation(rows_by_statement),
        _continuity(rows_by_statement, first),
        _official(rows_by_statement, figures),
        _basis_coverage(rows_by_statement, window),
        _disclosure(calendar, window),
        _freshness(rows_by_statement, today),
    ]
    if screening is not None and (screening.excluded or _old_breaks(checks)):
        checks.append(_excluded(screening, _old_breaks(checks)))
    return QualityReport(tuple(checks))


def _old_breaks(checks: list[QualityCheck]) -> list[dict]:
    continuity = next(c for c in checks if c.check_id == "Q-CONT")
    return [v for v in continuity.violations if v.get("outside_hard_window")]


def _excluded(screening: Screening, old_breaks: list[dict]) -> QualityCheck:
    """不拦，只把「哪些期被剔除了、哪些老年份不连续」摆出来。没有这类情况时不出现。"""
    listed = [{"kind": "excluded_period", **e} for e in screening.excluded] + [
        {"kind": "old_continuity_break", **b} for b in old_breaks
    ]
    return QualityCheck(
        "Q-OLD",
        "硬性拦截范围之前的数据：勾稽不平的报告期已剔除，不连续的年度已标注",
        False,
        False,
        len(listed),
        tuple(listed[:_MAX_LISTED]),
        f"硬性拦截的范围是 {screening.first_hard_year} 年及以后"
        f"（近 {screening.hard_years} 年）；剔除 {len(screening.excluded)} 期，"
        f"范围之前不连续的年度 {len(old_breaks)} 个",
    )


def _structure(rows_by_statement, window) -> QualityCheck:
    violations: list[dict] = []
    checked = 0
    for statement in STATEMENT_FIELDS:
        rows = rows_by_statement.get(statement) or []
        if not rows:
            violations.append({"statement": statement, "problem": "没有任何报告期"})
            continue
        seen: set[str] = set()
        for row in rows:
            checked += 1
            if row.report_date in seen:
                violations.append(
                    {
                        "statement": statement,
                        "report_date": row.report_date,
                        "problem": "报告期重复",
                    }
                )
            seen.add(row.report_date)
            if row.report_type == _ANNUAL and row.fiscal_year in window:
                for name in _REQUIRED[statement]:
                    if row.values.get(name) is None:
                        violations.append(
                            {
                                "statement": statement,
                                "report_date": row.report_date,
                                "field": name,
                                "problem": "必有字段为空",
                            }
                        )
    return QualityCheck(
        "Q-STRUCT",
        "结构：三张表都有数据，主键不重复，必有字段不为空",
        True,
        not violations,
        checked,
        tuple(violations[:_MAX_LISTED]),
    )


def _reconciliation(rows_by_statement) -> list[QualityCheck]:
    checks = []
    for rule in RECONCILIATION_RULES:
        violations: list[dict] = []
        checked = 0
        for row in rows_by_statement.get(rule.source_table) or []:
            residual = _residual(rule, row)
            if residual is None:
                continue
            checked += 1
            if abs(residual) > TOLERANCE_YUAN:
                violations.append(
                    {
                        "report_date": row.report_date,
                        "report_type": row.report_type,
                        "residual": round(residual, 2),
                    }
                )
        checks.append(
            QualityCheck(
                f"Q-{rule.rule_id}",
                f"勾稽：{rule.rule}",
                True,
                not violations and checked > 0,
                checked,
                tuple(violations[:_MAX_LISTED]),
                "" if checked else "没有一期具备这条规则所需的全部字段",
            )
        )
    return checks


def _continuity(rows_by_statement, first_hard_year: int | None = None) -> QualityCheck:
    """本年期初现金 = 上年期末现金。不连续必须能用口径变化解释，否则拦住。

    硬性拦截范围之前的年度不连续不拦：那些年度的口径本来就是未核实，只标注。
    """
    annual = sorted(
        (
            r
            for r in rows_by_statement.get("cash_flow") or []
            if r.report_type == _ANNUAL
        ),
        key=lambda r: r.fiscal_year,
    )
    explained: list[dict] = []
    unexplained: list[dict] = []
    old: list[dict] = []
    checked = 0
    for previous, current in zip(annual, annual[1:], strict=False):
        begin, end = current.values.get("begin_cce"), previous.values.get("end_cce")
        if (
            current.fiscal_year != previous.fiscal_year + 1
            or begin is None
            or end is None
        ):
            continue
        checked += 1
        diff = begin - end
        if abs(diff) <= TOLERANCE_YUAN:
            continue
        entry = {
            "fiscal_year": current.fiscal_year,
            "diff": round(diff, 2),
            "basis_previous": previous.basis,
            "basis_current": current.basis,
        }
        by_restatement = BASIS_RESTATED in (previous.basis, current.basis)
        if by_restatement:
            explained.append(entry)
        elif first_hard_year is not None and current.fiscal_year < first_hard_year:
            old.append({**entry, "outside_hard_window": True})
        else:
            unexplained.append(entry)
    notes = []
    if explained:
        notes.append(
            "不连续且已由追溯调整解释的年度："
            + "、".join(str(e["fiscal_year"]) for e in explained)
        )
    if old:
        notes.append(
            "硬性拦截范围之前不连续、只标注不拦的年度："
            + "、".join(str(e["fiscal_year"]) for e in old)
        )
    return QualityCheck(
        "Q-CONT",
        "跨期：本年期初现金 = 上年期末现金",
        True,
        not unexplained,
        checked,
        tuple((unexplained + explained + old)[:_MAX_LISTED]),
        "；".join(notes),
    )


def _official(rows_by_statement, figures) -> QualityCheck:
    """报表行标了口径的年份，关键科目必须与同口径的年报数一致。"""
    lookup: dict[tuple[str, int], StatementRow] = {}
    for statement, rows in rows_by_statement.items():
        for row in rows:
            if row.report_type == _ANNUAL:
                lookup[(statement, row.fiscal_year)] = row
    tables = {item.item: (item.table, item.field) for item in KEY_ITEMS}
    violations: list[dict] = []
    checked = 0
    for figure in figures:
        if figure.basis == BASIS_COMPARATIVE:
            continue
        table, name = tables[figure.item]
        row = lookup.get((table, figure.fiscal_year))
        if row is None or row.basis != figure.basis:
            continue
        if row.verified_against != figure.source_report:
            continue
        value = row.values.get(name)
        if value is None:
            continue
        checked += 1
        if abs(value - figure.value) > TOLERANCE_YUAN:
            violations.append(
                {
                    "fiscal_year": figure.fiscal_year,
                    "item": figure.item,
                    "basis": figure.basis,
                    "official": figure.value,
                    "dataset": value,
                    "source_report": figure.source_report,
                    "page": figure.page,
                }
            )
    return QualityCheck(
        "Q-OFFICIAL",
        "来源间一致：关键科目与年度报告原文逐项一致",
        True,
        not violations,
        checked,
        tuple(violations[:_MAX_LISTED]),
    )


def _basis_coverage(rows_by_statement, window) -> QualityCheck:
    """有年报原文的年份，报表的数必须与原文的某个口径一致；对不上就是来源间矛盾。

    年报没采到或没解析出来的年份不在此列：它们保持未核实，原因写在数据集说明里。
    """
    total = 0
    unverified: list[dict] = []
    counts = {BASIS_ORIGINAL: 0, BASIS_RESTATED: 0, BASIS_UNVERIFIED: 0}
    for statement, rows in rows_by_statement.items():
        for row in rows:
            if row.report_type != _ANNUAL or row.fiscal_year not in window:
                continue
            total += 1
            counts[row.basis] = counts.get(row.basis, 0) + 1
            if row.basis == BASIS_UNVERIFIED:
                unverified.append(
                    {"statement": statement, "fiscal_year": row.fiscal_year}
                )
    return QualityCheck(
        "Q-BASIS",
        "口径与来源一致：有年报原文的年份，报表与原文的某个口径一致",
        True,
        total > 0 and not unverified,
        total,
        tuple(unverified[:_MAX_LISTED]),
        f"判出口径 {total - len(unverified)}/{total}；{counts}",
    )


def _disclosure(calendar, window) -> QualityCheck:
    have = {e.fiscal_year for e in calendar}
    missing = [{"fiscal_year": y} for y in window if y not in have]
    return QualityCheck(
        "Q-DISCLOSURE",
        "时点：每个年度都有法定披露日",
        False,
        not missing,
        len(window),
        tuple(missing),
    )


def _freshness(rows_by_statement, today: date) -> QualityCheck:
    latest = max(
        (r.report_date for rows in rows_by_statement.values() for r in rows),
        default=None,
    )
    if latest is None:
        return QualityCheck("Q-FRESH", "新鲜度：最新一期不太旧", False, False, 0)
    age = (today - date.fromisoformat(latest)).days
    return QualityCheck(
        "Q-FRESH",
        "新鲜度：最新一期不太旧",
        False,
        age <= _STALE_DAYS,
        1,
        () if age <= _STALE_DAYS else ({"latest": latest, "age_days": age},),
        f"最新报告期 {latest}，距今 {age} 天",
    )
