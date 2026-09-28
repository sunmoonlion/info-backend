"""从年报的词和坐标里抽出三张合并报表的每一行。

输入是一页一页的词，不碰 PDF；读到三张表都结束就停，不把整份年报读完。
认不出来的地方记下原因并放弃那张表，不给出数（STMT-07）。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field

from app.application.securities.report_extraction.layout import (
    CJK,
    OTHER,
    STRONG,
    amount,
    columns,
    declared_unit_in,
    is_number,
    is_numeric,
    join_split_numbers,
    lines_of,
    statement_of,
    text_of,
    title_of,
    unit_in,
)
from app.domain.securities.report_statements import (
    STATEMENTS,
    UNIT_ABOVE_TITLE,
    UNIT_DECLARED_ELSEWHERE,
    UNIT_ON_STATEMENT,
    ExtractedRow,
    ExtractedStatement,
    ExtractionProblem,
    PageWords,
    ReportExtraction,
    Word,
)

MAX_SECTION_PAGES = 8
_MIN_AMOUNTS_PER_PAGE = 4
_COLUMN_TOLERANCE = 12.0


@dataclass
class _Section:
    statement: str
    start: int
    side_by_side: bool
    above: str | None  # 同一页上标题上方离得最近的单位
    declared: str | None
    unit: str | None = None  # 标题下面、第一行数字之前写的单位
    in_header: bool = True
    lines: list[tuple[int, list[Word]]] = field(default_factory=list)


def extract_statements(pages: Iterable[PageWords]) -> ReportExtraction:
    sections: dict[str, _Section] = {}
    problems: list[ExtractionProblem] = []
    done: set[str] = set()
    current: _Section | None = None
    declared: str | None = None
    pages_read = 0
    for page in pages:
        if len(done) == len(STATEMENTS) and current is None:
            break
        pages_read += 1
        page_lines = [join_split_numbers(found) for found in lines_of(page.words)]
        above: str | None = None
        for line in page_lines:
            raw = text_of(line)
            said = declared_unit_in(raw)
            declared = said or declared
            name, wide = statement_of(title_of(raw))
            if current is None:
                if name and name not in done and name not in sections:
                    current = _Section(name, page.page_number, wide, above, declared)
                    sections[name] = current
                elif not said:
                    # 「财务附注中报表的单位为」说的是附注，不是紧挨着的这张表
                    above = unit_in(raw) or above
                continue
            if name == current.statement:
                continue  # 续页上重复的标题
            if name or OTHER.match(title_of(raw)):
                if current.in_header:
                    # 标题下面一个数都没有就到了下一个标题：这是报表目录，不是报表
                    del sections[current.statement]
                else:
                    done.add(current.statement)
                current = None
                above = None
                if name and name not in done and name not in sections:
                    current = _Section(name, page.page_number, wide, above, declared)
                    sections[name] = current
                continue
            if page.page_number - current.start >= MAX_SECTION_PAGES:
                problems.append(
                    ExtractionProblem(
                        "section_too_long",
                        current.statement,
                        page.page_number,
                        f"超过 {MAX_SECTION_PAGES} 页还没结束",
                    )
                )
                current = None
                continue
            if current.in_header:
                # 单位写在表头里。第一行数字之后再出现的「人民币元」是别的意思（每股收益）
                if any(is_number(w.text) and STRONG.search(w.text) for w in line):
                    current.in_header = False
                elif current.unit is None:
                    current.unit = unit_in(raw)
            current.lines.append((page.page_number, line))
    statements: dict[str, ExtractedStatement] = {}
    failed = {p.statement for p in problems}
    for name in STATEMENTS:
        section = sections.get(name)
        if section is None:
            problems.append(ExtractionProblem("statement_not_found", name))
        elif name in failed:
            continue
        elif name not in done:
            problems.append(ExtractionProblem("section_end_not_found", name))
        else:
            built = _build(section)
            if isinstance(built, ExtractedStatement):
                statements[name] = built
            else:
                problems.extend(built)
    return ReportExtraction(statements, tuple(problems), pages_read)


def _build(section: _Section) -> ExtractedStatement | list[ExtractionProblem]:
    name = section.statement
    unit, source = section.unit, UNIT_ON_STATEMENT
    if unit is None:
        unit, source = section.above, UNIT_ABOVE_TITLE
    if unit is None:
        unit, source = section.declared, UNIT_DECLARED_ELSEWHERE
    if unit is None:
        return [ExtractionProblem("unit_not_stated", name, section.start)]
    per_page, problems = _columns_by_page(section)
    if problems:
        return problems
    if not per_page:
        return [ExtractionProblem("no_amounts", name, section.start)]
    rows: list[ExtractedRow] = []
    for number, line in section.lines:
        cols = per_page.get(number)
        label = "".join(w.text for w in line if CJK.search(w.text))
        numbers = [w for w in line if is_number(w.text) and not CJK.search(w.text)]
        broken = [
            w
            for w in line
            if cols
            and is_numeric(w.text)
            and STRONG.search(w.text)
            and not is_number(w.text)
            and (_at(w, cols[0]) or _at(w, cols[1]))
        ]
        if broken:
            return [ExtractionProblem("broken_amount", name, number, broken[0].text)]
        current = [w for w in numbers if cols and _at(w, cols[0])]
        prior = [w for w in numbers if cols and _at(w, cols[1])]
        if len(current) > 1 or len(prior) > 1:
            return [
                ExtractionProblem(
                    "two_amounts_in_one_column", name, number, text_of(line)[:40]
                )
            ]
        if cols is None and any(STRONG.search(w.text) for w in numbers):
            return [
                ExtractionProblem(
                    "amounts_on_a_page_without_columns",
                    name,
                    number,
                    text_of(line)[:40],
                )
            ]
        if not label and not current and not prior:
            continue
        rows.append(
            ExtractedRow(
                number,
                text_of([w for w in line if CJK.search(w.text)]),
                amount(current[0].text) if current else None,
                amount(prior[0].text) if prior else None,
            )
        )
    return ExtractedStatement(
        name, section.start, unit, source, section.side_by_side, tuple(rows), per_page
    )


def _at(word: Word, edge: float) -> bool:
    return abs(word.x1 - edge) <= _COLUMN_TOLERANCE


def _columns_by_page(
    section: _Section,
) -> tuple[dict[int, tuple[float, float]], list[ExtractionProblem]]:
    """同一张表跨页时每页的列位置可能不同，所以每页单独分列。"""
    per_page: dict[int, tuple[float, float]] = {}
    problems: list[ExtractionProblem] = []
    sparse: list[int] = []
    for number in sorted({n for n, _ in section.lines}):
        edges = [
            w.x1
            for n, line in section.lines
            if n == number
            for w in line
            if is_number(w.text) and STRONG.search(w.text)
        ]
        if len(edges) < _MIN_AMOUNTS_PER_PAGE:
            sparse.append(number)  # 这一页只有一两行：借相邻页的列位置
            continue
        found, why = columns(edges, 4 if section.side_by_side else 2)
        if found is None:
            problems.append(
                ExtractionProblem(why or "columns", section.statement, number)
            )
            continue
        per_page[number] = found
    for number in sparse:
        if not per_page:
            break
        nearest = min(per_page, key=lambda n: abs(n - number))
        if abs(nearest - number) == 1:
            per_page[number] = per_page[nearest]
    return per_page, problems
