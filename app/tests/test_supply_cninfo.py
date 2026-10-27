"""巨潮渠道插件（SDD 0045 第 3.2 节）：描述 → 公告分类与查询，列表 → 条目，取原件。

用的是从巨潮真实接口留下的夹具（上海机场的年度报告公告列表）。
"""

from __future__ import annotations

from datetime import date
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
    assert await catalog.list(DescriptionKey("600009.SH", "announcement")) == []


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
