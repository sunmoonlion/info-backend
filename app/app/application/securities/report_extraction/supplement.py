"""从定期报告附注的「现金流量表补充资料」抽折旧与摊销（10-28，为以后 DCF 这类估值备数）。

补充资料在附注里，合并报表之后：标题是「50、现金流量表补充资料」「（1）现金流量表补充资料」
「(1).现金流量表补充资料」这类，下面两列是本期、上期。只取这几行：固定资产折旧（含油气资产
折耗、生产性生物资产折旧）、使用权资产折旧、无形资产摊销、长期待摊费用摊销。科目名折行时
把相邻没有数的那一行拼上。认不出的不猜，照实记原因。

单位：补充资料下面写了就用它，没写就沿用同一份报告的合并现金流量表的单位。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal

from app.application.securities.report_extraction.layout import (
    CJK,
    STRONG,
    amount,
    columns,
    is_number,
    join_split_numbers,
    lines_of,
    text_of,
    unit_in,
)
from app.domain.securities.report_statements import PageWords, Word

MAX_SUPPLEMENT_PAGES = 3
_TITLE = re.compile(
    r"^([（(]?[一二三四五六七八九十\d]+[)）]?[、.．]?)*现金流量表补充资料([（(]续[)）])?$"
)
# 到了「现金及现金等价物净变动」那一段，折旧摊销早就过了
_END = re.compile(r"现金及现金等价物净变动|现金及现金等价物的构成|^[（(][2-9][)）]")
_COLUMN_TOLERANCE = 12.0

# 认出来的字段 → 判断的办法（去掉标点后的科目名）
FIELDS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "depreciation_fixed_assets",
        re.compile(r"^固定资产折旧(油气资产折耗)?(生产性生物资产(折旧)?)?$"),
    ),
    ("depreciation_right_of_use", re.compile(r"^使用权资产(折旧|摊销)$")),
    ("amortization_intangibles", re.compile(r"^无形资产摊销$")),
    ("amortization_long_term_prepaid", re.compile(r"^长期待摊费用摊销$")),
)
_PUNCTUATION = re.compile(r"[、，,（）()“”\s:：]|^(加|减)")


@dataclass(frozen=True)
class SupplementRow:
    page: int
    label: str
    current: Decimal | None
    prior: Decimal | None


@dataclass
class Supplement:
    start_page: int | None = None
    unit: str | None = None
    rows: list[SupplementRow] = field(default_factory=list)
    found: dict[str, SupplementRow] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


def field_of(label: str) -> str | None:
    text = _PUNCTUATION.sub("", label)
    for name, pattern in FIELDS:
        if pattern.match(text):
            return name
    return None


def extract_supplement(pages: Iterable[PageWords]) -> Supplement:
    out = Supplement()
    lines: list[tuple[int, list[Word]]] = []
    for page in pages:
        page_lines = [join_split_numbers(found) for found in lines_of(page.words)]
        for line in page_lines:
            raw = text_of(line)
            if out.start_page is None:
                if _TITLE.match(raw):
                    out.start_page = page.page_number
                continue
            if _END.search(raw) and lines:
                return _finish(out, lines)
            if out.unit is None and not lines:
                out.unit = unit_in(raw)
            lines.append((page.page_number, line))
        if out.start_page is not None and (
            page.page_number - out.start_page >= MAX_SUPPLEMENT_PAGES
        ):
            return _finish(out, lines)
    if out.start_page is None:
        out.problems.append("supplement_not_found")
        return out
    return _finish(out, lines)


def _finish(out: Supplement, lines: list[tuple[int, list[Word]]]) -> Supplement:
    edges = [
        w.x1
        for _, line in lines
        for w in line
        if is_number(w.text) and STRONG.search(w.text)
    ]
    cols, why = columns(edges, 2)
    if cols is None:
        out.problems.append(f"supplement_columns:{why}")
        return out
    labelled: list[tuple[int, str, Decimal | None, Decimal | None]] = []
    for page, line in lines:
        label = text_of([w for w in line if CJK.search(w.text)])
        numbers = [w for w in line if is_number(w.text) and not CJK.search(w.text)]
        current = [w for w in numbers if abs(w.x1 - cols[0]) <= _COLUMN_TOLERANCE]
        prior = [w for w in numbers if abs(w.x1 - cols[1]) <= _COLUMN_TOLERANCE]
        labelled.append(
            (
                page,
                label,
                amount(current[0].text) if len(current) == 1 else None,
                amount(prior[0].text) if len(prior) == 1 else None,
            )
        )
    used: set[int] = set()
    ambiguous: set[str] = set()

    def text_at(j: int) -> str:
        if not (0 <= j < len(labelled)) or j in used:
            return ""
        _, text, current, prior = labelled[j]
        return text if current is None and prior is None else ""

    for i, (page, label, current, prior) in enumerate(labelled):
        if current is None and prior is None:
            continue
        before, after = text_at(i - 1), text_at(i + 1)
        # 科目名折行：数字可能印在上一行、下一行，或上下两行文字的中间
        options: list[tuple[str, tuple[int, ...]]] = [(label, ())]
        if before:
            options.append((before + label, (i - 1,)))
        if after:
            options.append((label + after, (i + 1,)))
        if before and after:
            options.append((before + label + after, (i - 1, i + 1)))
        own, name = label, None
        for candidate, taken in options:
            name = field_of(candidate)
            if name is not None:
                own = candidate
                used.update(taken)
                break
        row = SupplementRow(page, own, current, prior)
        out.rows.append(row)
        if name is None or name in ambiguous:
            continue
        if name in out.found:
            out.problems.append(f"supplement_ambiguous:{name}")
            ambiguous.add(name)
            del out.found[name]
            continue
        out.found[name] = row
    return out
