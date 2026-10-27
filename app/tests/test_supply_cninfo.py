"""巨潮渠道插件（SDD 0045 第 3.2 节）：描述 → 公告分类与查询，列表 → 条目，取原件。

用的是从巨潮真实接口留下的夹具（上海机场的年度报告公告列表）。
"""

from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from app.application.supply.cninfo import CninfoCatalog
from app.domain.supply.models import DescriptionKey, SupplyError

FIXTURES = Path(__file__).parent / "fixtures" / "securities"


class FakeHttp:
    def __init__(self, pdf: bytes = b"%PDF-1.7 fake") -> None:
        self.pdf = pdf
        self.forms: list[dict[str, str]] = []

    async def post_form(self, url, form):
        self.forms.append(dict(form))
        return (FIXTURES / "cninfo_annual_query.json").read_bytes()

    async def get(self, url):
        return (FIXTURES / "cninfo_stock_list.json").read_bytes()

    async def download(self, url, *, max_bytes, timeout_seconds):
        return self.pdf, "application/pdf"


def annual(year: int, subject: str = "600009.SH") -> DescriptionKey:
    return DescriptionKey(
        subject, "annual_report", date(year, 1, 1), date(year, 12, 31)
    )


async def test_lists_the_full_report_of_that_year_only():
    http = FakeHttp()
    catalog = CninfoCatalog(http)
    entries = await catalog.list(annual(2025))
    assert [(e.title, e.channel_doc_id, e.announcement_id) for e in entries] == [
        ("上海机场2025年年度报告", "600009:annual_report:2025", "1225264362")
    ]
    assert (
        entries[0].url
        == "http://static.cninfo.com.cn/finalpage/2026-04-30/1225264362.PDF"
    )
    assert entries[0].disclosed_on == date(2026, 4, 30)
    form = http.forms[0]
    assert form["category"] == "category_ndbg_szsh"
    assert form["stock"].startswith("600009,")
    assert form["seDate"] == "2025-12-31~2027-06-30"
    # 别的年份、别的类型（没有这一类的条目）都不混进来
    assert [e.title for e in await catalog.list(annual(2021))] == [
        "上海机场2021年年度报告"
    ]
    assert (
        await catalog.list(
            DescriptionKey(
                "600009.SH", "q1_report", date(2025, 1, 1), date(2025, 3, 31)
            )
        )
        == []
    )
    # 不在词表里的类型、采不来的类型（研报）都是空
    assert await catalog.list(DescriptionKey("600009.SH", "announcement")) == []
    assert await catalog.list(DescriptionKey("600009.SH", "research_company")) == []


async def test_unknown_securities_and_non_pdfs_are_refused():
    catalog = CninfoCatalog(FakeHttp(pdf=b"<html>error</html>"))
    with pytest.raises(SupplyError) as caught:
        await catalog.list(annual(2025, "999999.SH"))
    assert caught.value.code == "security_not_found_at_source"
    entry = (await catalog.list(annual(2025)))[0]
    with pytest.raises(SupplyError) as caught:
        await catalog.fetch(entry)
    assert caught.value.code == "not_a_pdf"
    ok = CninfoCatalog(FakeHttp())
    content, media_type = await ok.fetch((await ok.list(annual(2025)))[0])
    assert content.startswith(b"%PDF-") and media_type == "application/pdf"


def row(n: int, title: str, day: str = "2026-05-08") -> dict:
    stamp = int(datetime.fromisoformat(f"{day}T10:00:00+08:00").timestamp() * 1000)
    return {
        "announcementId": str(1225000000 + n),
        "announcementTitle": title,
        "announcementTime": stamp,
        "adjunctUrl": f"finalpage/{day}/{1225000000 + n}.PDF",
    }


class CategoryHttp(FakeHttp):
    """按巨潮分类回不同的公告（同一条可以出现在两个分类里）。"""

    BY_CATEGORY = {
        "category_gqbd_szsh": [
            row(1, "上海机场关于回购股份的进展公告"),
            row(2, "上海机场关于股东权益变动的提示性公告（摘要）"),
        ],
        "category_gszl_szsh": [
            row(3, "上海机场投资者关系活动记录表"),
            row(4, "上海机场关于召开2025年度业绩说明会的公告"),
        ],
        "category_rcjy_szsh": [row(5, "上海机场关于重大资产重组的进展公告")],
        "": [
            row(5, "上海机场关于重大资产重组的进展公告"),
            row(6, "关于对上海机场的问询函"),
            row(7, "上海机场关于收到上海证券交易所问询函的公告"),
        ],
    }

    async def post_form(self, url, form):
        form = dict(form)
        self.forms.append(form)
        rows = self.BY_CATEGORY.get(form["category"], [])
        if form["category"] == "" and form["searchkey"]:
            rows = [r for r in rows if form["searchkey"] in r["announcementTitle"]]
        return json.dumps(
            {"announcements": rows, "totalAnnouncement": len(rows)}
        ).encode()


def window(doc_type: str, **kw) -> DescriptionKey:
    return DescriptionKey(
        "600009.SH", doc_type, date(2026, 1, 1), date(2026, 10, 1), **kw
    )


def test_titles_give_type_topics_and_edition():
    from app.application.supply.cninfo import edition_of, topics_of, type_of

    assert type_of("XX首次公开发行股票并在主板上市招股说明书") == "prospectus"
    assert type_of("XX首次公开发行股票上市公告书") == "listing_announcement"
    assert type_of("XX投资者关系活动记录表") == "ir_activity"
    assert type_of("关于对XX股份有限公司的问询函") == "regulatory_letter"
    assert type_of("XX关于收到问询函的公告") == "interim_announcement"
    assert topics_of("关于回购股份的公告", "category_gqbd_szsh") == ("equity",)
    assert topics_of("关于重大资产重组问询函的回复公告", "category_rcjy_szsh") == (
        "operations",
        "restructuring",
        "supervision",
    )
    assert topics_of("XX关于召开业绩说明会的公告", None) == ("results",)
    assert edition_of("2025年年度报告摘要") == "summary"
    assert edition_of("2025 Annual Report (English)") == "english"
    assert edition_of("2025年年度报告") == "full"


async def test_a_window_lists_each_announcement_with_its_type_and_topics():
    http = CategoryHttp()
    catalog = CninfoCatalog(http)
    # 要「股本与股东」的临时公告：只查股权激励、解禁、股权变动三个分类，摘要不算全文
    equity = await catalog.list(window("interim_announcement", topics=("equity",)))
    assert [(e.title, e.topics) for e in equity] == [
        ("上海机场关于回购股份的进展公告", ("equity",))
    ]
    assert equity[0].channel_doc_id == "600009:announcement:1225000001"
    assert {f["category"] for f in http.forms if f["category"]} == {
        "category_gqjl_szsh",
        "category_jj_szsh",
        "category_gqbd_szsh",
    }
    assert all(f["seDate"] == "2026-01-01~2026-10-01" for f in http.forms)
    # 只要投资者关系活动记录：只查公司治理
    http.forms.clear()
    ir = await catalog.list(window("ir_activity"))
    assert [e.doc_type for e in ir] == ["ir_activity"]
    assert [f["category"] for f in http.forms] == ["category_gszl_szsh"]
    # 「重组与收购」没有对应分类：按关键词检索，同一条在两处出现只算一份、事项合起来
    restructuring = await catalog.list(
        window("interim_announcement", topics=("restructuring",))
    )
    assert [e.announcement_id for e in restructuring] == ["1225000005"]
    # 大类「公告」不带事项：逐个分类查，再按关键词补；原函是政策法规·监管函件，
    # 不在公告大类里
    everything = await catalog.list(window("disclosure"))
    kinds = {e.title: (e.doc_type, e.topics) for e in everything}
    assert kinds["上海机场关于召开2025年度业绩说明会的公告"] == (
        "interim_announcement",
        ("governance", "results"),
    )
    assert kinds["上海机场关于重大资产重组的进展公告"] == (
        "interim_announcement",
        ("operations", "restructuring"),
    )
    assert kinds["上海机场关于收到上海证券交易所问询函的公告"][1] == ("supervision",)
    assert "关于对上海机场的问询函" not in kinds
    letters = await catalog.list(window("regulatory_letter"))
    assert [e.doc_type for e in letters] == ["regulatory_letter"]
    summary = await catalog.list(window("interim_announcement", edition="summary"))
    assert [e.edition for e in summary] == ["summary"]
