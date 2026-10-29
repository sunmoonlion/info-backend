"""巨潮互动易插件（互动问答，0049 第三节）：列窗里已回答的问答、取一条排成 Markdown。

回复的样子照 2026-10-29 实测互动易接口的字段（只留用到的几个，内容是编的）。
"""

from __future__ import annotations

import json
from datetime import date

import pytest

from app.application.supply.cninfo_irm import CninfoIrmCatalog
from app.domain.supply.models import DescriptionKey, SupplyError

# 2026-10-27 16:34（北京时间）回答；2026-10-15 回答；一条没回答
ROWS = [
    {
        "indexId": "2385012367278190592",
        "mainContent": "最近公司是否有积极变化",
        "pubDate": 1792387983000,
        "updateDate": 1793090043000,
        "stockCode": "000001",
        "companyShortName": "平安银行",
        "authorName": "irm3510729",
        "attachedContent": "您好。本行已于2026年10月24日披露三季度报告。",
    },
    {
        "indexId": "2351508701685010432",
        "mainContent": "建议公司把投行做大",
        "pubDate": 1788487644000,
        "updateDate": 1792068304000,
        "stockCode": "000001",
        "companyShortName": "平安银行",
        "authorName": "cninfo712105",
        "attachedContent": "您好。感谢您的建议。",
    },
    {
        "indexId": "2390000000000000000",
        "mainContent": "还没人回答的问题",
        "pubDate": 1793090043000,
        "updateDate": 1793090043000,
        "stockCode": "000001",
        "companyShortName": "平安银行",
        "authorName": "x",
        "attachedContent": None,
    },
]
DETAIL = {
    "statusCode": 200,
    "data": {
        "questionContent": "最近公司是否有积极变化",
        "questioner": "irm3510729",
        "questionDate": 1792387983000,
        "stockCode": "000001",
        "shortName": "平安银行",
        "replyContent": "您好。本行已于2026年10月24日披露三季度报告。",
        "replyDate": 1793090043000,
    },
}


class FakeHttp:
    def __init__(self) -> None:
        self.forms: list[tuple[str, dict]] = []
        self.downloads: list[str] = []
        self.lookup = [{"stockCode": "000001", "secid": "gssz0000001"}]

    async def post_form(self, url, form):
        self.forms.append((url, dict(form)))
        if url.endswith("queryKeyboardInfo"):
            return json.dumps({"data": self.lookup}).encode()
        return json.dumps({"total": 3, "rows": ROWS}).encode()

    async def get(self, url):
        raise AssertionError("取一份要走 download（算日上限）")

    async def download(self, url, *, max_bytes, timeout_seconds):
        self.downloads.append(url)
        return json.dumps(DETAIL).encode(), "application/json"


def window(start: date | None = None, end: date | None = None) -> DescriptionKey:
    return DescriptionKey("000001.SZ", "ir_qa", start, end)


async def test_lists_answered_questions_in_the_window():
    http = FakeHttp()
    catalog = CninfoIrmCatalog(http, "cninfo-irm")
    entries = await catalog.list(window(date(2026, 10, 1), date(2026, 10, 31)))
    assert [(e.channel_doc_id, e.disclosed_on) for e in entries] == [
        ("000001:irm:2385012367278190592", date(2026, 10, 27)),
        ("000001:irm:2351508701685010432", date(2026, 10, 15)),
    ]
    first = entries[0]
    assert (first.channel_id, first.doc_type, first.edition) == (
        "cninfo-irm",
        "ir_qa",
        "full",
    )
    assert first.title.startswith("平安银行互动易问答：最近公司")
    assert first.url.endswith("questionId=2385012367278190592")
    assert http.forms[1][1]["orgId"] == "gssz0000001"
    # 窗外的不要；大类「公告」也算要互动问答；别的类型不列
    assert len(await catalog.list(window(date(2026, 10, 20), date(2026, 10, 31)))) == 1
    assert len(await catalog.list(DescriptionKey("000001.SZ", "disclosure"))) == 2
    assert await catalog.list(DescriptionKey("000001.SZ", "annual_report")) == []


async def test_a_company_not_on_the_platform_has_none():
    http = FakeHttp()
    http.lookup = [{"stockCode": "000002", "secid": "gssz0000002"}]
    assert await CninfoIrmCatalog(http).list(window()) == []


async def test_fetch_writes_one_question_and_answer_as_markdown():
    http = FakeHttp()
    catalog = CninfoIrmCatalog(http, "cninfo-irm")
    [entry, _] = await catalog.list(window(date(2026, 10, 1), date(2026, 10, 31)))
    content, media_type = await catalog.fetch(entry)
    text = content.decode()
    assert media_type == "text/markdown; charset=utf-8"
    assert text.startswith("# 平安银行（000001）互动易问答\n")
    assert "- 回答时间：2026-10-27 16:34" in text
    assert "## 问\n\n最近公司是否有积极变化" in text
    assert "## 答（平安银行）\n\n您好。本行已于" in text
    # 不写提问者的账号
    assert "irm3510729" not in text
    assert http.downloads == [
        "https://irm.cninfo.com.cn/newircs/question/getQuestionDetail"
        "?questionId=2385012367278190592"
    ]
    # 同一条问答每次排出来一字不差（交 knowbase 按内容摘要认同一份）
    again, _ = await catalog.fetch(entry)
    assert again == content


async def test_unexpected_replies_are_errors():
    class Broken(FakeHttp):
        async def post_form(self, url, form):
            return b"<html>busy</html>"

    with pytest.raises(SupplyError) as caught:
        await CninfoIrmCatalog(Broken()).list(window())
    assert caught.value.code == "unexpected_response"
