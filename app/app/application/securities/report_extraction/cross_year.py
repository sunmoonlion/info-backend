"""跨年核对：同一年的数，当年年报的本期列与下一年年报的上期列应该相同。

勾稽只管得到参与等式的那十几个科目。别的科目读错了，勾稽发现不了；
而每个科目在相邻两份年报里各出现一次，这是不依赖第三方数据的另一份证据。

两份年报的数不同，不一定是读错：重述、重新归类也会不同。这里只负责把不同的找出来、
把最像读错的两种情形（后半截、差整数量级）单独标出，不替人下结论。
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from app.application.securities.report_extraction.labels import normalize, strip_note
from app.domain.securities.report_statements import (
    AGREE,
    AMBIGUOUS,
    DIFFERENT,
    ONLY_EARLIER,
    ONLY_LATER,
    PAIRED_BY_VALUE,
    SIGN_ONLY,
    SUSPECT_TRUNCATED,
    SUSPECT_UNIT,
    CrossYearItem,
    CrossYearResult,
    ExtractedStatement,
)

# 单位认错时两个数恰好差一千、一万、一百万倍（千元、万元、百万元），允许百分之一的出入
_UNIT_FACTORS = (Decimal(1000), Decimal(10000), Decimal(1000000))
_UNIT_SLACK = Decimal("0.01")
_MIN_TRUNCATED_DIGITS = 2
_PER_SHARE = "每股"  # 每股收益以元每股计，不随报表的单位变
_UNLABELLED = "（无标签）"
# 按数配对只用于够大的数：小的数（零、几元、每股收益）碰巧相同的可能太大
_MIN_VALUE_TO_PAIR = Decimal(1000)


def _by_label(
    statement: ExtractedStatement, column: str
) -> dict[str, list[tuple[Decimal, int]]]:
    """归一化之后的科目名 →［（换算成元的数，页码）］，按出现的先后。"""
    found: dict[str, list[tuple[Decimal, int]]] = defaultdict(list)
    for row in statement.rows:
        value = getattr(row, column)
        if value is None:
            continue
        label = normalize(strip_note(row.label)) or _UNLABELLED
        factor = Decimal(1) if _PER_SHARE in row.label else statement.factor
        found[label].append((value * factor, row.page))
    return found


def _digits(value: Decimal) -> str:
    """整数部分的数字。比较后半截时不看小数和符号。"""
    return str(abs(value).to_integral_value(rounding="ROUND_DOWN"))


def _is_tail(short: str, long: str) -> bool:
    return (
        len(short) >= _MIN_TRUNCATED_DIGITS
        and len(short) < len(long)
        and long.endswith(short)
    )


def judge(earlier: Decimal, later: Decimal, tolerance: Decimal) -> str:
    """两个已换算成元的数是什么关系。tolerance 是两份年报里较粗的那个单位。"""
    if abs(earlier - later) <= tolerance:
        return AGREE
    if abs(abs(earlier) - abs(later)) <= tolerance:
        return SIGN_ONLY
    small, large = sorted((abs(earlier), abs(later)))
    short, long = sorted((_digits(earlier), _digits(later)), key=len)
    if _is_tail(short, long):
        return SUSPECT_TRUNCATED
    if small > 0:
        ratio = large / small
        if any(abs(ratio / f - 1) <= _UNIT_SLACK for f in _UNIT_FACTORS):
            return SUSPECT_UNIT
    return DIFFERENT


def cross_check(
    earlier: ExtractedStatement,
    later: ExtractedStatement,
    *,
    fiscal_year: int,
) -> CrossYearResult:
    """earlier 是 fiscal_year 那一年的年报，later 是下一年的年报，两者是同一张表。

    按归一化之后的科目名配对。同名的行按出现的先后配；两边同名行的数量不同就不配，
    记为配不上，不猜哪一行对哪一行。没有科目名的行也不配。
    每股收益这类以元每股计的行不乘报表的单位。
    """
    if earlier.statement != later.statement:
        raise ValueError("cross_check needs the same statement from both reports")
    tolerance = max(earlier.factor, later.factor)
    ours = _by_label(earlier, "current")
    theirs = _by_label(later, "prior")
    items: list[CrossYearItem] = []

    def item(label, n, a, b, verdict, page_a=None, page_b=None) -> None:
        items.append(
            CrossYearItem(
                earlier.statement, fiscal_year, label, n, a, b, verdict, page_a, page_b
            )
        )

    for label in sorted(set(ours) | set(theirs)):
        mine = ours.get(label, [])
        other = theirs.get(label, [])
        unpairable = label == _UNLABELLED or len(mine) != len(other)
        if mine and other and unpairable:
            for n, (value, page) in enumerate(mine, 1):
                item(label, n, value, None, AMBIGUOUS, page, None)
            for n, (value, page) in enumerate(other, 1):
                item(label, n, None, value, AMBIGUOUS, None, page)
            continue
        if not other:
            for n, (value, page) in enumerate(mine, 1):
                item(label, n, value, None, ONLY_EARLIER, page, None)
            continue
        if not mine:
            for n, (value, page) in enumerate(other, 1):
                item(label, n, None, value, ONLY_LATER, None, page)
            continue
        for n, ((a, page_a), (b, page_b)) in enumerate(
            zip(mine, other, strict=True), 1
        ):
            item(label, n, a, b, judge(a, b, tolerance), page_a, page_b)
    return CrossYearResult(
        earlier.statement, fiscal_year, tuple(_pair_by_value(items, tolerance))
    )


def _pair_by_value(
    items: list[CrossYearItem], tolerance: Decimal
) -> list[CrossYearItem]:
    """科目名对不上的行，如果数相同、而且这个数在两边剩下的行里都只出现一次，就是同一个科目。

    科目名对不上多半是折行：同一个科目名在两份年报里断在不同的地方。
    """
    unpaired = (ONLY_EARLIER, ONLY_LATER, AMBIGUOUS)

    def bucket(value: Decimal) -> Decimal:
        return (value / tolerance).to_integral_value()

    left: dict[Decimal, list[CrossYearItem]] = defaultdict(list)
    right: dict[Decimal, list[CrossYearItem]] = defaultdict(list)
    for entry in items:
        if entry.verdict not in unpaired:
            continue
        value = entry.earlier if entry.earlier is not None else entry.later
        if value is None or abs(value) < _MIN_VALUE_TO_PAIR:
            continue
        (left if entry.earlier is not None else right)[bucket(value)].append(entry)
    used: set[int] = set()
    paired: list[CrossYearItem] = []
    for key, ours in left.items():
        theirs = right.get(key, [])
        if len(ours) != 1 or len(theirs) != 1:
            continue
        a, b = ours[0], theirs[0]
        used.update((id(a), id(b)))
        paired.append(
            CrossYearItem(
                a.statement,
                a.fiscal_year,
                a.label,
                a.occurrence,
                a.earlier,
                b.later,
                AGREE,
                a.earlier_page,
                b.later_page,
                PAIRED_BY_VALUE,
                b.label,
            )
        )
    return [entry for entry in items if id(entry) not in used] + paired
