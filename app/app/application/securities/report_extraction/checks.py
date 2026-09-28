"""抽出来的数敢不敢用：同期勾稽，以及与参照数的量级核对。

勾稽在年报自己的单位上做，容差是一个单位：以千元列示的表，各项四舍五入之后差一千元以内算平。
勾稽发现不了单位认错（整张表同比例放大仍然是平的），所以单位要另外用参照数核对（STMT-08）。
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from app.domain.securities.financial_catalog import RECONCILIATION_RULES
from app.domain.securities.report_statements import (
    ExtractedStatement,
    Imbalance,
    Resolution,
)

COLUMNS = ("current", "prior")
_TOLERANCE_UNITS = Decimal(1)
_SAME = Decimal(10)  # 十倍以内算同一量级：重述、合并范围变化会让数变，但变不了这么多
_UNIT = Decimal(100)  # 单位认错至少差一千倍；留出余量，一百倍以上就算

MAGNITUDE_SAME = "same"
MAGNITUDE_UNIT = "unit_mismatch"
MAGNITUDE_DIFFERENT = "different"


def values_of(resolution: Resolution, column: str) -> dict[str, Decimal]:
    if column not in COLUMNS:
        raise ValueError(f"unknown column: {column}")
    found = {}
    for name, line in resolution.lines.items():
        value = getattr(line.row, column)
        if value is not None:
            found[name] = value
    return found


def _residual(terms, values: Mapping[str, Decimal]) -> Decimal | None:
    total = Decimal(0)
    for name, sign, optional in terms:
        value = values.get(name)
        if value is None:
            if optional:
                continue
            return None
        total += sign * value
    return total


def reconcile(
    statement: ExtractedStatement, resolution: Resolution
) -> tuple[int, tuple[Imbalance, ...]]:
    """返回（做了几次检查，不平的）。规则所需的科目不全时那一次不算。"""
    checked = 0
    broken: list[Imbalance] = []
    for column in COLUMNS:
        values = values_of(resolution, column)
        for rule in RECONCILIATION_RULES:
            if rule.source_table != statement.statement:
                continue
            residual = _residual(rule.terms, values)
            if residual is None:
                continue
            checked += 1
            if abs(residual) <= _TOLERANCE_UNITS:
                continue
            flipped = tuple(
                name
                for name, _, optional in rule.terms
                if optional
                and name in values
                and abs(
                    _residual(rule.terms, {**values, name: -values[name]}) or Decimal(0)
                )
                <= _TOLERANCE_UNITS
            )
            broken.append(Imbalance(rule.rule_id, column, residual, flipped))
    return checked, tuple(broken)


def magnitude(ours_in_yuan: Decimal, reference_in_yuan: Decimal) -> str:
    """同一个科目，我们抽的数与参照数差多少倍。

    十倍以内算同一量级；差一百倍以上是单位认错了（千元、万元、百万元认成了元，或反过来）；
    十倍到一百倍之间说不清。只看大小，不看正负号。
    """
    if reference_in_yuan == 0 or ours_in_yuan == 0:
        if ours_in_yuan == reference_in_yuan:
            return MAGNITUDE_SAME
        return MAGNITUDE_DIFFERENT
    ratio = abs(ours_in_yuan / reference_in_yuan)
    if ratio < 1:
        ratio = 1 / ratio
    if ratio <= _SAME:
        return MAGNITUDE_SAME
    return MAGNITUDE_UNIT if ratio >= _UNIT else MAGNITUDE_DIFFERENT


def check_unit(
    statement: ExtractedStatement,
    resolution: Resolution,
    reference_in_yuan: Mapping[str, Decimal],
    column: str = "current",
) -> tuple[str, dict[str, str]]:
    """用参照数核对单位。返回（结论，每个科目的结论）。

    结论：confirmed 有参照且量级都对；unit_mismatch 多数科目恰好差整数量级；
    inconsistent 有对有错；no_reference 没有可比的科目。
    """
    ours = values_of(resolution, column)
    verdicts = {
        name: magnitude(ours[name] * statement.factor, reference)
        for name, reference in reference_in_yuan.items()
        if name in ours
    }
    if not verdicts:
        return "no_reference", verdicts
    kinds = set(verdicts.values())
    if kinds == {MAGNITUDE_SAME}:
        return "confirmed", verdicts
    wrong = sum(1 for v in verdicts.values() if v == MAGNITUDE_UNIT)
    if wrong * 2 > len(verdicts) and MAGNITUDE_SAME not in kinds:
        return "unit_mismatch", verdicts
    return "inconsistent", verdicts
