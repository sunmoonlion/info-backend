"""认科目：把年报上印的科目名对到财务目录的字段上。

只认目录里有的和下面列出的别名。对不上的行原样保留，不猜；同一个字段对上不止一行时都不用。
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from app.domain.securities.financial_catalog import STATEMENT_FIELDS, YUAN
from app.domain.securities.report_statements import (
    ExtractedRow,
    Resolution,
    ResolvedLine,
)

# 附注编号的几种写法：（六十一）、五（一）、七、1、十七、（3）、六.12
_NOTE = re.compile(
    r"([一二三四五六七八九十]+[、.．]?)?([（(][一二三四五六七八九十百零\d]+[)）]|\d+)$"
)
_PREFIX = re.compile(
    r"^([一二三四五六七八九十]+、|[（(][一二三四五六七八九十]+[)）]|\d+[.．、]"
    r"|其中：|加：|减：)+"
)
_PAREN = re.compile(r"[（(][^（）()]*(填列|或股东权益|或股本|元/股)[^（）()]*[)）]?")
_PUNCTUATION = re.compile(r"[（）()“”\"－\-\s:：]")
_MIN_PREFIX_MATCH = 10

# 年报里常见、而目录的中文名没有覆盖的叫法
ALIASES: dict[str, dict[str, str]] = {
    "balance_sheet": {
        "实收资本": "share_capital",
        "股本": "share_capital",
        "实收资本股本": "share_capital",
        "股东权益合计": "total_equity",
        "归属于母公司股东权益合计": "total_parent_equity",
        "归属于母公司股东的权益合计": "total_parent_equity",
        "负债和股东权益总计": "total_liab_equity",
        "负债及股东权益总计": "total_liab_equity",
    },
    "income_statement": {
        "归属于母公司股东的净利润": "parent_netprofit",
        "所得税费用": "income_tax",
    },
    "cash_flow": {
        "汇率变动对现金及现金等价物的影响": "rate_change_effect",
        "年初现金及现金等价物余额": "begin_cce",
        "年末现金及现金等价物余额": "end_cce",
    },
}


def strip_note(label: str) -> str:
    return _NOTE.sub("", label)


def normalize(label: str) -> str:
    """去掉附注编号、序号、「其中：」、括号里的填列说明和标点；「所有者」统一成「股东」。"""
    text = _PAREN.sub("", _PREFIX.sub("", _NOTE.sub("", label)))
    return _PUNCTUATION.sub("", text).replace("所有者", "股东")


def catalog_names(statement: str) -> dict[str, str]:
    """归一化之后的科目名 → 字段。只收以元计的字段。"""
    names = {
        normalize(display): name
        for name, _, display, unit in STATEMENT_FIELDS[statement]
        if unit == YUAN
    }
    fields = {name for name, _, _, _ in STATEMENT_FIELDS[statement]}
    names.update({k: v for k, v in ALIASES.get(statement, {}).items() if v in fields})
    return names


def match(names: dict[str, str], label: str) -> str | None:
    key = normalize(label)
    if key in names:
        return names[key]
    if len(key) >= _MIN_PREFIX_MATCH:
        hits = {f for name, f in names.items() if name.startswith(key)}
        if len(hits) == 1:
            return hits.pop()
    return None


def resolve(statement: str, rows: Sequence[ExtractedRow]) -> Resolution:
    """科目名折成两三行时，数字印在其中一行上：把相邻的、没有数字的行拼上再认。

    先试拼接，再试单独这一行：「归属于母公司股东的／净利润」的第二行单独也对得上
    「净利润」，先试单行会把它认错。一行没有数字的文字只能借给一个科目。
    """
    names = catalog_names(statement)
    found: dict[str, ResolvedLine | None] = {}
    unmatched: list[str] = []
    used: set[int] = set()

    def text_at(index: int) -> str:
        if index < 0 or index >= len(rows) or index in used:
            return ""
        row = rows[index]
        return row.label if row.current is None and row.prior is None else ""

    for i, row in enumerate(rows):
        if row.current is None and row.prior is None:
            continue
        before, after = text_at(i - 1), text_at(i + 1)
        own = strip_note(row.label)
        options: list[tuple[str, tuple[int, ...]]] = []
        if before:
            options.append((before + own, (i - 1,)))
        if after:
            options.append((own + after, (i + 1,)))
        if before and after:
            options.append((before + own + after, (i - 1, i + 1)))
        options.append((own, ()))
        exact = names.get(normalize(own)) if own else None
        name, taken = None, ()
        for candidate, neighbours in options:
            matched = match(names, candidate) if candidate else None
            if matched is None:
                continue
            if neighbours and matched == exact:
                continue  # 这一行自己就是完整的科目名：旁边那行不是它的，不占用
            name, taken = matched, neighbours
            break
        used.update(taken)
        if name is None:
            unmatched.append(normalize(own) or "（无标签）")
            continue
        found[name] = None if name in found else ResolvedLine(name, row, 1 + len(taken))
    return Resolution(
        {k: v for k, v in found.items() if v is not None},
        tuple(k for k, v in found.items() if v is None),
        tuple(unmatched),
    )
