"""版面：把词拼成行，认标题，认单位，按右边缘分列。都是纯函数。"""

from __future__ import annotations

import re
from collections.abc import Sequence
from decimal import Decimal

from app.domain.securities.report_statements import Word

WANTED = {
    "合并资产负债表": "balance_sheet",
    "合并利润表": "income_statement",
    "合并现金流量表": "cash_flow",
}
# 这些标题出现，说明前一张合并报表结束了
OTHER = re.compile(
    r"^(母公司|公司|本公司)?(资产负债表|利润表|现金流量表|所有者权益变动表|股东权益变动表)$"
    r"|^合并(所有者权益变动表|股东权益变动表)$"
)
# 合并与母公司并排印在一张表里
COMBINED = re.compile(r"^合并及(母公司|公司)(资产负债表|利润表|现金流量表)$")
COMBINED_NAME = {
    "资产负债表": "balance_sheet",
    "利润表": "income_statement",
    "现金流量表": "cash_flow",
}

NUMBER = re.compile(
    r"^[-－—]?[（(]?[-－]?\d{1,3}(,\d{3})*(\.\d+)?[)）]?$"
    r"|^[-－—]?[（(]?[-－]?\d+(\.\d+)?[)）]?$"
)
STRONG = re.compile(r"[,.]")  # 带千分位或小数点的，才拿来判断列的位置
# 只由数字、千分位、小数点、负号、括号组成的词：可能是一个数，也可能是一个数的碎片
_NUMERIC = re.compile(r"^[-－—（(]*[\d,.]+[)）]*$")
_LEADING_ZERO = re.compile(r"^[-－—（(]*0\d*,")  # 「0,419」：千分位前面不会以零开头
_TOUCHING = 1.0  # 两个词的间隙不超过这么多点，算贴在一起
CJK = re.compile(r"[一-鿿]")

_UNIT_LINE = re.compile(
    r"单位为?[:：]?(人民币)?(百万元|千元|万元|元)|人民币(百万元|千元|万元|元)"
)
# 有的年报只在财务报表开头说一次「财务附注中报表的单位为：千元」
_DECLARED = re.compile(r"报表的?单位为?[:：]?(人民币)?(百万元|千元|万元|元)")
_NUMBERING = re.compile(
    r"^(\d+[、.．]|[（(][一二三四五六七八九十\d]+[)）]|[一二三四五六七八九十]+、)"
)
_PERIOD = re.compile(r"^\d{4}年(度|\d{1,2}月\d{1,2}日)?")
_CONTINUED = re.compile(r"[（(]续[)）]$")
_SPACE = re.compile(r"\s+")

_BIN = 8.0  # 右边缘按 8 点一格归堆
_APART = 30.0  # 两列的右边缘至少隔这么远
_NEAR_COLUMN = 12.0  # 右边缘离一列的位置这么近，就算这一列的数
_MAX_TITLE = 14


def squeeze(text: str) -> str:
    return _SPACE.sub("", text)


def lines_of(words: Sequence[Word]) -> list[list[Word]]:
    """同一高度的词是一行。行内从左到右。"""
    rows: dict[int, list[Word]] = {}
    for word in words:
        rows.setdefault(round(word.top / 3), []).append(word)
    merged: list[list[Word]] = []
    for key in sorted(rows):
        line = sorted(rows[key], key=lambda w: w.x0)
        if merged and abs(line[0].top - merged[-1][0].top) < 3:
            merged[-1] = sorted(merged[-1] + line, key=lambda w: w.x0)
        else:
            merged.append(line)
    return merged


def text_of(line: Sequence[Word]) -> str:
    return squeeze("".join(w.text for w in line))


def title_of(joined: str) -> str:
    """认标题前去掉序号、印在同一行的期间、续页的「(续)」。

    「1、合并资产负债表」「2024年度合并及公司利润表」「合并现金流量表(续)」。
    去掉之后不以「表」结尾或太长，就不是标题，原样返回。
    """
    stripped = _CONTINUED.sub("", _PERIOD.sub("", _NUMBERING.sub("", joined)))
    if stripped.endswith("表") and len(stripped) <= _MAX_TITLE:
        return stripped
    return joined


def statement_of(title: str) -> tuple[str | None, bool]:
    """标题对应哪张表，是不是并排的版式。"""
    both = COMBINED.match(title)
    if both:
        return COMBINED_NAME[both.group(2)], True
    return WANTED.get(title), False


def unit_in(text: str) -> str | None:
    matched = _UNIT_LINE.search(text)
    if matched is None:
        return None
    return matched.group(2) or matched.group(3)


def declared_unit_in(text: str) -> str | None:
    matched = _DECLARED.search(text)
    return matched.group(2) if matched else None


def is_number(text: str) -> bool:
    return NUMBER.match(text) is not None and _LEADING_ZERO.match(text) is None


def is_numeric(text: str) -> bool:
    """看上去是数或数的碎片。"""
    return _NUMERIC.match(text) is not None


def join_split_numbers(line: Sequence[Word]) -> list[Word]:
    """PDF 里一个数偶尔被拆成两个贴在一起的词（「21,65」「0,419」）：拼回去。

    只拼贴在一起的、都由数字和千分位组成的词。拼出来是不是合法的数，由后面判断。
    """
    joined: list[Word] = []
    for word in line:
        last = joined[-1] if joined else None
        if (
            last is not None
            and is_numeric(last.text)
            and is_numeric(word.text)
            and word.x0 - last.x1 <= _TOUCHING
        ):
            joined[-1] = Word(last.text + word.text, last.x0, word.x1, last.top)
        else:
            joined.append(word)
    return joined


def amount(token: str) -> Decimal:
    """年报上印的数。括号和负号都表示负数。"""
    text = (
        token.replace(",", "")
        .replace("（", "(")
        .replace("）", ")")
        .replace("－", "-")
        .replace("—", "-")
    )
    negative = "-" in text or "(" in text
    value = Decimal(text.strip("()-"))
    return -value if negative else value


def columns(
    right_edges: Sequence[float], wanted: int = 2
) -> tuple[tuple[float, float] | None, str | None]:
    """数字右对齐：右边缘最集中的两处是本期、上期两列。

    并排的版式有四列，左边两列是合并报表的本期与上期。认不出来就说原因，不猜。
    一列有多少个数，按落在它附近的右边缘来数，不按单独一格：同一列的右边缘
    恰好跨在两格的分界上时，单独一格会少算。
    """
    bins: dict[int, list[float]] = {}
    for x in right_edges:
        bins.setdefault(round(x / _BIN), []).append(x)
    centres = sorted(((len(v), sum(v) / len(v)) for v in bins.values()), reverse=True)
    places: list[float] = []
    for _, centre in centres:
        if all(abs(centre - c) > _APART for c in places):
            places.append(centre)
    picked = [
        (sum(1 for x in right_edges if abs(x - centre) <= _NEAR_COLUMN), centre)
        for centre in places
    ]
    if wanted == 4:
        dense = [c for n, c in picked if n >= 0.3 * picked[0][0]]
        if len(dense) != 4:
            return None, f"expected_four_columns_found_{len(dense)}"
        left = sorted(dense)[:2]
        return (left[0], left[1]), None
    if len(picked) < 2:
        return None, "one_column"
    if len(picked) >= 3 and picked[2][0] >= 0.4 * picked[0][0]:
        return None, "more_than_two_columns"
    first, second = sorted(c for _, c in picked[:2])
    return (first, second), None
