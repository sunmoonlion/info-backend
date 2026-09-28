"""从年度报告原文抽出的三张合并报表（0008-info-statements 段七）。

这里的数是年报上印的原样：没有乘单位，没有改符号。金额用精确十进制。
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

STATEMENTS = ("balance_sheet", "income_statement", "cash_flow")

UNIT_FACTORS: dict[str, Decimal] = {
    "元": Decimal(1),
    "千元": Decimal(1000),
    "万元": Decimal(10000),
    "百万元": Decimal(1000000),
}

# 单位是在哪里认到的。只有第一种是报表自己写明的；后两种要用参照数核对量级之后才能用
UNIT_ON_STATEMENT = "statement_page"  # 标题下面、第一行数字之前
UNIT_ABOVE_TITLE = "above_title"  # 同一页上标题的上方
UNIT_DECLARED_ELSEWHERE = "declared_elsewhere"  # 只在财务报表开头说过一次


@dataclass(frozen=True)
class Word:
    """页面上的一个词和它的位置。坐标的单位是 PDF 的点，原点在页面左上角。"""

    text: str
    x0: float
    x1: float
    top: float


@dataclass(frozen=True)
class PageWords:
    page_number: int  # 从 1 起
    words: tuple[Word, ...]


@dataclass(frozen=True)
class ExtractedRow:
    page: int
    label: str  # 去掉空白之后的原样科目名，可能带序号和附注编号
    current: Decimal | None  # 本期列
    prior: Decimal | None  # 上期列


@dataclass(frozen=True)
class ExtractedStatement:
    statement: str
    start_page: int
    unit: str
    unit_source: str
    side_by_side: bool  # 合并与母公司并排印在一张表里；这里只取合并的两列
    rows: tuple[ExtractedRow, ...]
    columns: dict[int, tuple[float, float]]  # 页码 → 本期、上期两列的右边缘

    @property
    def factor(self) -> Decimal:
        return UNIT_FACTORS[self.unit]


@dataclass(frozen=True)
class ExtractionProblem:
    """没抽出来或不敢用的原因。code 是稳定的错误码。"""

    code: str
    statement: str | None
    page: int | None = None
    detail: str = ""


@dataclass(frozen=True)
class ReportExtraction:
    statements: dict[str, ExtractedStatement]
    problems: tuple[ExtractionProblem, ...]
    pages_read: int

    @property
    def complete(self) -> bool:
        return all(name in self.statements for name in STATEMENTS)


@dataclass(frozen=True)
class ResolvedLine:
    """认出了科目的一行。field 是财务目录里的字段名。"""

    field: str
    row: ExtractedRow
    label_lines: int  # 科目名占了几行（折行时大于 1）


@dataclass(frozen=True)
class Resolution:
    lines: dict[str, ResolvedLine]
    ambiguous: tuple[str, ...]  # 同一个字段对上了不止一行：都不用
    unmatched: tuple[str, ...]  # 带数字但目录里没有的科目，归一化之后的名字


@dataclass(frozen=True)
class Imbalance:
    rule_id: str
    column: str  # current / prior
    residual: Decimal  # 年报上的单位
    balanced_if_flipped: tuple[
        str, ...
    ] = ()  # 把这些科目的符号反过来就平：列报习惯的线索


# 跨年核对：同一年的同一个科目，当年年报的「本期」与下一年年报的「上期」
AGREE = "agree"  # 两份年报给的数相同
SIGN_ONLY = "sign_only"  # 大小相同、正负号相反：列报习惯变了
SUSPECT_TRUNCATED = "suspect_truncated"  # 一个数是另一个数的后半截：多半是读错了
SUSPECT_UNIT = "suspect_unit"  # 差一百倍以上：多半是单位认错了
DIFFERENT = "different"  # 两份年报给的数不同：重述、重新归类，或读错
ONLY_EARLIER = "only_earlier"  # 只有当年年报里有
ONLY_LATER = "only_later"  # 只有下一年年报里有
AMBIGUOUS = "ambiguous"  # 同名的行两边数量不同，配不上
PAIRED_BY_LABEL = "label"
PAIRED_BY_VALUE = "value"


@dataclass(frozen=True)
class CrossYearItem:
    statement: str
    fiscal_year: int  # 这个数属于哪一年
    label: str  # 归一化之后的科目名
    occurrence: int  # 同名的行里第几个，从 1 起
    earlier: Decimal | None  # 当年年报本期列，已换算成元
    later: Decimal | None  # 下一年年报上期列，已换算成元
    verdict: str
    earlier_page: int | None = None
    later_page: int | None = None
    paired_by: str = (
        "label"  # label：科目名相同；value：科目名对不上，数相同且两边都只此一个
    )
    later_label: str | None = None  # 按数配对时，下一年年报里的科目名


@dataclass(frozen=True)
class CrossYearResult:
    statement: str
    fiscal_year: int
    items: tuple[CrossYearItem, ...]

    def count(self, verdict: str) -> int:
        return sum(1 for item in self.items if item.verdict == verdict)

    @property
    def compared(self) -> int:
        return sum(
            1 for i in self.items if i.earlier is not None and i.later is not None
        )

    @property
    def suspects(self) -> tuple[CrossYearItem, ...]:
        return tuple(
            i for i in self.items if i.verdict in (SUSPECT_TRUNCATED, SUSPECT_UNIT)
        )
