"""正文抽取与描述识别（SDD 0045 第 3.4 节）。"""

from __future__ import annotations

from datetime import date

import pytest

from app.application.supply.readings import form_of, recognize_periodic, text_reading

PAGE = """<html><head><title>关于印发某办法的通知_政策</title>
<meta name="PubDate" content="2026-10-01 10:00"></head>
<body><nav>首页 | 政策 | 解读</nav>
<article><h1>关于印发某办法的通知</h1>
<p>各省、自治区、直辖市人民政府：现将某办法印发给你们，请认真贯彻执行。这一段是正文，
写得长一些，免得被当成导航或广告。</p>
<p>第二段正文同样写得长一些。附件：某办法。本办法自发布之日起施行。</p></article>
<footer>版权所有 联系我们</footer></body></html>"""


def test_a_web_page_gives_its_body_title_and_date():
    reading = text_reading(PAGE.encode(), "text/html; charset=utf-8")
    assert "认真贯彻执行" in reading.body and "版权所有" not in reading.body
    assert reading.title and "某办法" in reading.title
    assert reading.published_on == date(2026, 10, 1)
    # 渠道列表给的标题、日期比从页面猜的可靠
    given = text_reading(
        PAGE.encode(), "text/html", title="渠道给的标题", published_on=date(2026, 9, 30)
    )
    assert (given.title, given.published_on) == ("渠道给的标题", date(2026, 9, 30))


def test_text_and_markdown_are_their_own_body():
    md = text_reading("﻿# 调研纪要\n\n去了工厂。".encode(), "text/markdown")
    assert (md.title, md.body) == ("调研纪要", "# 调研纪要\n\n去了工厂。")
    plain = text_reading("一段话。".encode(), "text/plain; charset=utf-8")
    assert (plain.title, plain.body) == (None, "一段话。")
    for content, media_type in (
        (b"   ", "text/plain"),
        (b"\xff\xfe", "text/plain"),
        (b"%PDF-1.7", "application/pdf"),
    ):
        with pytest.raises((ValueError, UnicodeDecodeError)):
            text_reading(content, media_type)


def test_forms_by_media_type():
    assert form_of("application/pdf") == "pdf"
    assert form_of("text/markdown; charset=utf-8") == "text"
    assert form_of("text/csv; charset=utf-8") == "table"
    assert form_of("application/zip") is None


def cover(*texts: str) -> list[dict[str, object]]:
    return [{"page": n + 1, "text": t} for n, t in enumerate(texts)]


def test_the_cover_tells_a_periodic_report():
    key = recognize_periodic(
        cover(
            "",
            "证券代码：600009 证券简称：上海机场\n上海国际机场股份有限公司\n2025年半年度报告",
        )
    )
    assert key is not None
    assert (key.subject, key.doc_type, key.period_start, key.period_end) == (
        "600009.SH",
        "semiannual_report",
        date(2025, 1, 1),
        date(2025, 6, 30),
    )
    q3 = recognize_periodic(cover("股票代码:000858\n2026 年第三季度报告"))
    assert q3 is not None and (q3.doc_type, q3.period_end) == (
        "q3_report",
        date(2026, 9, 30),
    )
    # 摘要不抽表；没有证券代码、不是定期报告的认不出
    assert recognize_periodic(cover("证券代码：600009\n2025年年度报告摘要")) is None
    assert recognize_periodic(cover("2025年年度报告")) is None
    assert recognize_periodic(cover("证券代码：600009\n关于召开股东会的通知")) is None
