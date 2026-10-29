"""正文抽取与描述识别（SDD 0045 第 3.4 节的两个通用加工器）。

- **正文**（`text`）：网页去导航、广告，转 Markdown，取标题、发布日期（用 trafilatura，
  和旧通用采集同一个库，市面上做正文抽取的常用开源库）；文本、Markdown 本来就是正文，
  照原样写回，Markdown 的头一个一级标题当标题。写回 knowbase 的格式见 knowbase 契约说明
  「读法写回格式」：请求体是 UTF-8 文字，可带 `title`、`published_on`。
- **描述识别**（只为决定要不要抽表）：上传的 PDF 没有渠道列表给的描述，从封面几页认
  「证券代码」与「某年年度报告」这类标题，认出是定期报告全文才抽三张合并报表。认出来
  的描述只是「自动识别未确认」，不写回 knowbase（knowbase 没有收描述的接口，描述由本人在
  knowbase 填）；认不出就不抽，照实写原因。

表格整理（Excel、CSV 转统一的表结构）knowbase 还没有写回格式（表格读法只收财报事实行），
这一版照实写「没成」，见交回报告。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

from app.domain.securities.models import InvalidSecurityCode, SecurityCode
from app.domain.supply.models import DescriptionKey

TEXT_VERSION = "text-1"
MAX_TITLE = 500
MAX_TEXT_BYTES = 20 * 1024 * 1024

# 内容类型 → 用哪种加工
_PDF = ("application/pdf",)
_HTML = ("text/html", "application/xhtml+xml")
_TEXT = ("text/plain", "text/markdown")
_TABLE = (
    "text/csv",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
)


def form_of(media_type: str) -> str | None:
    """pdf、html、text、table，认不得的回 None。"""
    base = media_type.split(";", 1)[0].strip().lower()
    for form, types in (
        ("pdf", _PDF),
        ("html", _HTML),
        ("text", _TEXT),
        ("table", _TABLE),
    ):
        if base in types:
            return form
    return None


@dataclass(frozen=True)
class TextReading:
    body: str
    title: str | None = None
    published_on: date | None = None


def text_reading(
    content: bytes,
    media_type: str,
    *,
    title: str | None = None,
    published_on: date | None = None,
) -> TextReading:
    """正文读法。`title`、`published_on` 是渠道列表给的（有就用它，比从内容猜的可靠）。

    抽不出正文（空的、不是 UTF-8、不是网页或文本）抛 ValueError，调用方照实写「没成」。"""
    form = form_of(media_type)
    if len(content) > MAX_TEXT_BYTES:
        raise ValueError("too large for a text reading")
    if form == "html":
        reading = _from_html(content)
    elif form == "text":
        reading = _from_text(content, markdown="markdown" in media_type)
    else:
        raise ValueError("no text reading for this kind of content")
    if not reading.body.strip():
        raise ValueError("empty text")
    return TextReading(
        body=reading.body,
        title=_clip(title) or reading.title,
        published_on=published_on or reading.published_on,
    )


def _from_text(content: bytes, *, markdown: bool) -> TextReading:
    text = content.decode("utf-8").lstrip("﻿").strip()
    found = None
    if markdown:
        heading = re.search(r"^#\s+(.+?)\s*#*\s*$", text, re.MULTILINE)
        found = _clip(heading.group(1)) if heading else None
    return TextReading(body=text, title=found)


def _from_html(content: bytes) -> TextReading:
    import trafilatura  # noqa: PLC0415 - 只有抽网页正文时才要

    html = content.decode("utf-8", errors="replace")
    document = trafilatura.bare_extraction(
        html,
        output_format="python",
        include_tables=True,
        include_links=False,
        with_metadata=True,
    )
    body = trafilatura.extract(
        html, output_format="markdown", include_tables=True, include_links=False
    )
    found_title = getattr(document, "title", None) if document is not None else None
    found_date = getattr(document, "date", None) if document is not None else None
    return TextReading(
        body=body or "",
        title=_clip(found_title),
        published_on=_date(found_date),
    )


def _clip(value: str | None) -> str | None:
    if not value:
        return None
    value = " ".join(value.split())
    return value[:MAX_TITLE] or None


def _date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


# —— 描述识别：上传的 PDF 是不是定期报告全文 ——

_CODE = re.compile(r"(?:证券|股票|A\s*股)\s*代码\s*[:：]?\s*(\d{6})")
# 细类 → 封面标题、报告期的起止（月、日）
_PERIODIC_TITLES: tuple[tuple[str, re.Pattern[str], tuple[int, int]], ...] = (
    (
        "q1_report",
        re.compile(r"(20\d{2})\s*年\s*第一季度报告"),
        (3, 31),
    ),
    (
        "q3_report",
        re.compile(r"(20\d{2})\s*年\s*第三季度报告"),
        (9, 30),
    ),
    (
        "semiannual_report",
        re.compile(r"(20\d{2})\s*年\s*(?:半年度报告|中期报告)"),
        (6, 30),
    ),
    (
        "annual_report",
        re.compile(r"(20\d{2})\s*年\s*(?:年度报告|年报报告)"),
        (12, 31),
    ),
)
COVER_PAGES = 3


def recognize_periodic(pages: list[dict[str, object]]) -> DescriptionKey | None:
    """从封面几页认定期报告全文：证券代码 + 标题里的年份与种类。摘要、英文版不认（只有
    全文抽表，同渠道来的规矩）。认不出回 None。"""
    cover = "\n".join(str(p.get("text") or "") for p in pages[:COVER_PAGES])
    code_match = _CODE.search(cover)
    if code_match is None:
        return None
    try:
        code = SecurityCode(code_match.group(1))
    except InvalidSecurityCode:
        return None
    best: tuple[int, str, int, tuple[int, int]] | None = None
    for doc_type, pattern, (month, day) in _PERIODIC_TITLES:
        found = pattern.search(cover)
        if found and (best is None or found.start() < best[0]):
            best = (found.start(), doc_type, int(found.group(1)), (month, day))
    if best is None:
        return None
    start, doc_type, year, (month, day) = best
    end = cover.find("\n", start)
    title_line = cover[start : end if end != -1 else None]
    if any(word in title_line.lower() for word in ("摘要", "英文", "english")):
        return None
    return DescriptionKey(
        f"{code.code}.{code.market}",
        doc_type,
        date(year, 1, 1),
        date(year, month, day),
    )
