"""通用网页型、订阅源型、搜索型适配器与渲染客户端（SDD 0045 第 3.2 节；发版前补齐 F4）；
主体按 knowbase 词表认机构编号；巨潮、互动易的两处修正（独立审第 3、8 条）。不连网，取数换成
记录请求的假的。"""

from __future__ import annotations

import base64
import json
from datetime import date, datetime

import httpx
import pytest

from app.application.ports.supply import Page
from app.application.supply import cninfo as cninfo_module
from app.application.supply.cninfo import CninfoCatalog
from app.application.supply.cninfo_irm import CninfoIrmCatalog
from app.application.supply.watch import recent_periods, watch_catalog
from app.application.supply.web import WebSite, feed_items, links_in
from app.domain.supply.catalog import normalize_url, web_doc_id
from app.domain.supply.models import (
    Channel,
    DemandItemIn,
    DescriptionKey,
    Entry,
    SupplyError,
    check_demand,
    check_subject,
)
from app.domain.supply.subscription import check_search, check_subscription
from app.infrastructure.external.knowbase_supply import HttpKnowbase
from app.infrastructure.external.knowbase_user import HttpKnowbaseUser
from app.infrastructure.external.render_client import HttpRenderer

ORG = "org:11100000000013127N"  # 国家统计局的统一社会信用代码（写法照词表）


def channel(**kw) -> Channel:
    base = dict(
        id="site-www-stats-gov-cn",
        name="国家统计局",
        kind="web",
        plugin="web",
        doc_types=("statistical_bulletin",),
        readings=("original", "text"),
        license="via_expert",
        review="approved",
        rate_per_minute=6,
        daily_cap=200,
        priority=100,
        domain="stats.gov.cn",
    )
    base.update(kw)
    return Channel(**base)  # type: ignore[arg-type]


class FakeHttp:
    """按网址回页面；没登记的网址回 404。记下每次问了什么、算不算一份。"""

    def __init__(self, pages: dict[str, Page]) -> None:
        self.pages = pages
        self.asked: list[tuple[str, bool]] = []

    async def page(self, url, *, max_bytes, timeout_seconds, fetch, site=None):
        self.asked.append((url, fetch))
        return self.pages.get(url, Page(404, "text/html", b"", url))


def html(body: str, url: str, status: int = 200) -> Page:
    return Page(status, "text/html; charset=utf-8", body.encode(), url)


class FakeRenderer:
    def __init__(self, configured: bool) -> None:
        self._configured = configured
        self.rendered: list[str] = []

    def configured(self) -> bool:
        return self._configured

    async def render(self, url, *, max_bytes, timeout_seconds):
        self.rendered.append(url)
        return html("<html><body>渲染后的</body></html>", url)


@pytest.fixture(autouse=True)
def fresh_robots(monkeypatch):
    from app.application.supply import web

    monkeypatch.setattr(web, "_ROBOTS", {})


PAGE = "https://www.stats.gov.cn/sj/zxfb/202610/t1.html"
ROBOTS = "https://www.stats.gov.cn/robots.txt"


async def test_a_page_is_fetched_only_on_its_site_and_robots_are_obeyed():
    http = FakeHttp(
        {
            ROBOTS: Page(
                200, "text/plain", b"User-agent: *\nDisallow: /private/\n", ROBOTS
            ),
            PAGE: html("<p>公报</p>", PAGE),
        }
    )
    site = WebSite(http, channel())
    content, media_type, final = await site.fetch_url(PAGE)
    assert content == "<p>公报</p>".encode() and final == PAGE
    # robots 问一次（不算一份），页面算一份
    assert http.asked == [(ROBOTS, False), (PAGE, True)]
    with pytest.raises(SupplyError) as caught:
        await site.fetch_url("https://www.stats.gov.cn/private/a.html")
    assert caught.value.code == "robots_disallowed"
    with pytest.raises(SupplyError) as caught:
        await site.fetch_url("https://www.example.com/a.html")
    assert caught.value.code == "off_site"
    # 审核定了不守 robots 的渠道不问 robots
    loose = WebSite(FakeHttp({PAGE: html("x", PAGE)}), channel(obey_robots=False))
    await loose.fetch_url(PAGE)
    assert loose.http.asked == [(PAGE, True)]  # type: ignore[attr-defined]


async def test_robots_missing_means_allowed_and_robots_failing_means_wait():
    http = FakeHttp({PAGE: html("x", PAGE)})  # robots.txt 404
    assert (await WebSite(http, channel()).fetch_url(PAGE))[0] == b"x"
    from app.application.supply import web

    web._ROBOTS.clear()
    broken = FakeHttp({ROBOTS: Page(503, "text/plain", b"", ROBOTS)})
    with pytest.raises(SupplyError) as caught:
        await WebSite(broken, channel()).fetch_url(PAGE)
    assert (caught.value.status, caught.value.code) == (503, "robots_unreachable")


async def test_statuses_map_to_absent_rate_limited_and_failed():
    for status, expected in (
        (404, (404, "page_not_found")),
        (403, (422, "channel_refused")),
        (429, (429, "channel_rate_limited")),
        (502, (503, "channel_unavailable")),
    ):
        http = FakeHttp({PAGE: html("", PAGE, status)})
        with pytest.raises(SupplyError) as caught:
            await WebSite(http, channel(obey_robots=False)).fetch_url(PAGE)
        assert (caught.value.status, caught.value.code) == expected
    # 重定向到别的网站的不认（那个网站没审过）
    moved = FakeHttp({PAGE: html("x", "https://evil.example.com/x")})
    with pytest.raises(SupplyError) as caught:
        await WebSite(moved, channel(obey_robots=False)).fetch_url(PAGE)
    assert caught.value.code == "off_site"


async def test_rendering_needs_review_config_and_a_render_service():
    http = FakeHttp({PAGE: html("<div id=app></div>", PAGE)})
    wants = channel(render_allowed=True, config={"render": True}, obey_robots=False)
    with pytest.raises(SupplyError) as caught:
        await WebSite(http, wants, renderer=FakeRenderer(False)).fetch_url(PAGE)
    assert caught.value.code == "render_unavailable"
    renderer = FakeRenderer(True)
    content, _, _ = await WebSite(http, wants, renderer=renderer).fetch_url(PAGE)
    assert "渲染后的" in content.decode() and renderer.rendered == [PAGE]
    # 审核没放行渲染的，配置写了也不渲染；文件不渲染
    plain = channel(render_allowed=False, config={"render": True}, obey_robots=False)
    assert (await WebSite(http, plain, renderer=renderer).fetch_url(PAGE))[0]
    assert renderer.rendered == [PAGE]


def test_linked_files_stay_on_site_one_level_and_at_most_twenty():
    links = "".join(f'<a href="/files/{n}.pdf">报告{n}</a>' for n in range(25))
    body = (
        links
        + '<a href="https://other.example.com/x.pdf">外站</a>'
        + '<a href="/sj/page.html">网页</a><a href="#top">顶</a>'
    ).encode()
    site = WebSite(FakeHttp({}), channel())
    taken, rest = site.linked_files(body, PAGE)
    assert len(taken) == 20 and rest == 5
    assert taken[0] == ("https://www.stats.gov.cn/files/0.pdf", "报告0")
    custom = WebSite(FakeHttp({}), channel(config={"follow_suffixes": [".html"]}))
    assert [u for u, _ in custom.linked_files(body, PAGE)[0]] == [
        "https://www.stats.gov.cn/sj/page.html"
    ]


def test_feeds_rss_and_atom_and_not_feeds():
    rss = b"""<?xml version="1.0"?><rss version="2.0"><channel>
    <item><title>A</title><link>https://www.stats.gov.cn/a.html</link>
    <pubDate>Tue, 27 Oct 2026 08:00:00 +0800</pubDate></item>
    <item><title>B</title><guid>https://www.stats.gov.cn/b.html</guid></item>
    </channel></rss>"""
    assert feed_items(rss) == [
        ("https://www.stats.gov.cn/a.html", "A", date(2026, 10, 27)),
        ("https://www.stats.gov.cn/b.html", "B", None),
    ]
    atom = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>C</title>
    <link href="https://www.stats.gov.cn/c.html"/><updated>2026-10-28T00:00:00Z</updated>
    </entry></feed>"""
    assert feed_items(atom) == [
        ("https://www.stats.gov.cn/c.html", "C", date(2026, 10, 28))
    ]
    assert feed_items(b"<html><body>no</body></html>") is None
    # 带文档类型声明的不认（挡外部实体）
    assert feed_items(b'<!DOCTYPE rss [<!ENTITY x "y">]><rss></rss>') is None


async def test_watching_a_feed_and_a_column_page_keeps_a_shared_cursor():
    feed_url = "https://www.stats.gov.cn/rss.xml"
    rss = b"""<rss><channel><item><title>A</title><link>/a.html</link>
    <pubDate>Tue, 27 Oct 2026 08:00:00 +0800</pubDate></item></channel></rss>"""
    http = FakeHttp({feed_url: Page(200, "application/rss+xml", rss, feed_url)})
    site = WebSite(
        http,
        channel(kind="feed", plugin="feed", config={"subject": ORG}, obey_robots=False),
    )
    entries, cursor = await site.watch(feed_url, {})
    assert [(e.url, e.disclosed_on, e.subject, e.doc_type) for e in entries] == [
        (
            "https://www.stats.gov.cn/a.html",
            date(2026, 10, 27),
            ORG,
            "statistical_bulletin",
        )
    ]
    again, _ = await site.watch(feed_url, cursor)
    assert again == []
    # 栏目页：只认列表位置的链接；第一次看到的、没有日期的标成起点
    column = "https://www.stats.gov.cn/sj/zxfb/"
    page = html(
        '<a href="/about.html">关于</a><a href="/sj/zxfb/t1.html">一</a>'
        '<a href="/sj/zxfb/t2.html">二</a>',
        column,
    )
    watcher = WebSite(
        FakeHttp({column: page}),
        channel(
            kind="feed",
            plugin="feed",
            config={"link_pattern": "/sj/zxfb/t"},
            obey_robots=False,
        ),
    )
    entries, cursor = await watcher.watch(column, {})
    assert [e.url for e in entries] == [
        "https://www.stats.gov.cn/sj/zxfb/t1.html",
        "https://www.stats.gov.cn/sj/zxfb/t2.html",
    ]
    assert set(cursor["baseline"]) == {e.channel_doc_id for e in entries}  # type: ignore[arg-type]


async def test_site_search_keeps_the_original_order():
    search_url = "https://www.stats.gov.cn/search?q=%E5%B7%A5%E4%B8%9A"
    page = html(
        '<a href="/r/2.html">第二个结果在前</a><a href="/nav.html">导航</a>'
        '<a href="/r/1.html">第一个</a>',
        search_url,
    )
    site = WebSite(
        FakeHttp({search_url: page}),
        channel(
            kind="search",
            plugin="site-search",
            config={
                "search_url": "https://www.stats.gov.cn/search?q={q}",
                "result_pattern": "/r/",
            },
            obey_robots=False,
        ),
    )
    found = await site.search("工业", 10)
    assert [c["url"] for c in found] == [
        "https://www.stats.gov.cn/r/2.html",
        "https://www.stats.gov.cn/r/1.html",
    ]
    with pytest.raises(SupplyError) as caught:
        await WebSite(FakeHttp({}), channel(kind="search")).search("x", 1)
    assert caught.value.code == "search_not_configured"


def test_urls_normalize_and_web_ids_follow_them():
    assert normalize_url("HTTPS://WWW.Stats.gov.cn:443/a?b=1#frag") == (
        "https://www.stats.gov.cn/a?b=1"
    )
    assert web_doc_id("https://www.stats.gov.cn") == web_doc_id(
        "https://www.stats.gov.cn/"
    )
    assert links_in(b'<a href="b.html">b</a>', "https://x.gov.cn/a/", "x.gov.cn") == [
        ("https://x.gov.cn/a/b.html", "b")
    ]


def test_subjects_follow_the_knowbase_vocabulary():
    check_subject("600519.SH", "annual_report")
    check_subject(ORG, "statistical_bulletin")
    check_subject("org:kb-stats-bureau", "policy")
    for subject, doc_type, code in (
        ("org:not-a-code", "law", "subject_unsupported"),
        ("600519", "annual_report", "subject_unsupported"),
        # 机构编号只配政策法规、统计数据
        (ORG, "annual_report", "subject_type_mismatch"),
    ):
        with pytest.raises(SupplyError) as caught:
            check_subject(subject, doc_type)
        assert caught.value.code == code
    # 需求项：描述或网址，两样只给一样
    with pytest.raises(SupplyError) as caught:
        check_demand(
            "alice",
            "user",
            "idem-0001-abcd",
            [DemandItemIn(DescriptionKey(ORG, "law"), ("original",), "c1", url=PAGE)],
        )
    assert caught.value.code == "item_invalid"
    check_demand(
        "alice",
        "user",
        "idem-0001-abcd",
        [DemandItemIn(None, ("original",), "c1", url=PAGE)],
    )


def test_subscription_and_search_limits():
    today = date(2026, 10, 30)
    ok = dict(
        by="description",
        subjects=("600519.SH",),
        doc_types=("annual_report",),
        urls=(),
        topic=None,
        readings=("original",),
        starts_on=today,
        ends_on=date(2027, 10, 30),
        every_hours=24,
        monthly_cap=10,
        purpose="跟踪年报",
        today=today,
    )
    check_subscription(**ok)  # type: ignore[arg-type]
    for change, code in (
        ({"ends_on": date(2027, 11, 1)}, "ends_on_invalid"),
        ({"starts_on": date(2026, 10, 1)}, "starts_on_invalid"),
        ({"every_hours": 0}, "every_invalid"),
        ({"subjects": (ORG,)}, "subject_type_mismatch"),
        ({"by": "url", "urls": ("ftp://x",)}, "url_invalid"),
        ({"by": "topic", "topic": " "}, "topic_invalid"),
    ):
        with pytest.raises(SupplyError) as caught:
            check_subscription(**{**ok, **change})  # type: ignore[arg-type]
        assert caught.value.code == code
    assert check_search(
        "工业", ("www.stats.gov.cn", "https://www.stats.gov.cn/x"), 5
    ) == ("www.stats.gov.cn",)


class ListingCatalog:
    def __init__(self) -> None:
        self.keys: list[DescriptionKey] = []

    async def list(self, key):
        self.keys.append(key)
        return [
            Entry(
                "cninfo",
                f"{key.subject}:{key.doc_type}:{key.period_end}",
                "t",
                "u",
                key.period_end,
                announcement_id="1",
                doc_type=key.doc_type,
            )
        ]


async def test_watching_a_catalog_lists_recent_periods_and_windows():
    assert recent_periods("annual_report", date(2026, 10, 30)) == [
        date(2025, 12, 31),
        date(2024, 12, 31),
    ]
    catalog = ListingCatalog()
    found, cursor = await watch_catalog(
        catalog, "600519.SH|q3_report", {}, date(2026, 10, 30)
    )
    assert [k.period_end for _, k in found] == [date(2026, 9, 30), date(2025, 9, 30)]
    again, _ = await watch_catalog(
        catalog, "600519.SH|q3_report", cursor, date(2026, 10, 30)
    )
    assert again == []
    catalog.keys.clear()
    await watch_catalog(
        catalog,
        "600519.SH|interim_announcement",
        {"since": "2026-10-20"},
        date(2026, 10, 30),
    )
    assert (catalog.keys[0].period_start, catalog.keys[0].period_end) == (
        date(2026, 10, 17),
        date(2026, 10, 30),
    )


class ListHttp:
    """巨潮的证券列表：第一次没有新上市的那家，第二次有。"""

    def __init__(self) -> None:
        self.calls = 0

    async def get(self, url):
        self.calls += 1
        stocks = [{"code": "600519", "orgId": "gssh0600519"}]
        if self.calls > 1:
            stocks.append({"code": "688999", "orgId": "gshk0688999"})
        return json.dumps({"stockList": stocks}).encode()


async def test_cninfo_refreshes_the_security_list_once_for_an_unknown_code(monkeypatch):
    """新上市的公司缓存里没有：先重取一次列表再判定（独立审第 3 条）；十分钟内不为它再取。"""
    monkeypatch.setattr(cninfo_module, "_ORG_IDS", {"at": None, "ids": {}})
    http = ListHttp()
    catalog = CninfoCatalog(http)  # type: ignore[arg-type]
    monkeypatch.setattr(catalog, "_stock_list", lambda: _stocks(http))
    assert await catalog._org_id("600519") == "gssh0600519"
    assert http.calls == 1
    # 缓存刚取过：不重取，判定「巨潮没有」
    with pytest.raises(SupplyError) as caught:
        await CninfoCatalog(http)._org_id("688999")  # type: ignore[arg-type]
    assert caught.value.status == 404
    # 缓存旧了十分钟以上：重取一次，就认得了
    cninfo_module._ORG_IDS["at"] = (
        datetime.now().astimezone() - cninfo_module.ORG_IDS_RECHECK
    )  # type: ignore[index]
    fresh = CninfoCatalog(http)  # type: ignore[arg-type]
    monkeypatch.setattr(fresh, "_stock_list", lambda: _stocks(http))
    assert await fresh._org_id("688999") == "gshk0688999"


async def _stocks(http):
    return json.loads(await http.get("list"))["stockList"]


class IrmHttp:
    """互动易：五页问答，第三页起都早于时间窗。"""

    def __init__(self) -> None:
        self.pages: list[str] = []

    async def post_form(self, url, form):
        values = dict(form)
        if url.endswith("queryKeyboardInfo"):
            return json.dumps(
                {"data": [{"stockCode": "000858", "secid": "gssz0000858"}]}
            ).encode()
        page = int(values["pageNum"])
        self.pages.append(values["pageNum"])
        day = {1: 1793404800000, 2: 1790726400000}.get(page, 1700000000000)
        rows = [
            {
                "indexId": str(page * 10 + n),
                "attachedContent": "答",
                "mainContent": "问",
                "pubDate": day,
                "updateDate": day,
            }
            for n in range(2)
        ]
        return json.dumps({"rows": rows, "totalPage": 5}).encode()


async def test_irm_pages_through_until_older_than_the_window():
    """原来只取第一页（独立审第 8 条）：按 totalPage 往后翻，整页早于窗口起点就停。"""
    http = IrmHttp()
    found = await CninfoIrmCatalog(http).list(  # type: ignore[arg-type]
        DescriptionKey("000858.SZ", "ir_qa", date(2026, 9, 1), date(2026, 12, 31))
    )
    # 第三页起整页的回答都早于窗口：多翻一页的余量，连着两页旧页才停（二审第 18 条）
    assert http.pages == ["1", "2", "3", "4"]
    assert len(found) == 4


async def test_render_client_checks_the_url_and_reads_the_answer():
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "status": 200,
                "final_url": "https://93.184.216.34/",
                "content_type": "text/html; charset=utf-8",
                "body_base64": base64.b64encode("页面".encode()).decode(),
            },
        )

    renderer = HttpRenderer(
        base_url="http://render.knowfeed.svc:8080",
        timeout_seconds=30,
        transport=httpx.MockTransport(handler),
    )
    page = await renderer.render(
        "https://93.184.216.34/", max_bytes=1000, timeout_seconds=10
    )
    assert page.content == "页面".encode() and page.status == 200
    assert seen == [
        {"url": "https://93.184.216.34/", "max_bytes": 1000, "timeout_seconds": 10}
    ]
    # 内网地址先在这头挡掉，不交给渲染服务
    with pytest.raises(SupplyError):
        await renderer.render("http://127.0.0.1/", max_bytes=1000, timeout_seconds=10)
    assert len(seen) == 1
    off = HttpRenderer(base_url="", timeout_seconds=30)
    assert not off.configured()
    with pytest.raises(SupplyError) as caught:
        await off.render("https://93.184.216.34/", max_bytes=1, timeout_seconds=1)
    assert caught.value.code == "render_unavailable"


async def test_knowbase_user_client_sends_the_user_token():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.method == "DELETE":
            return httpx.Response(404, json={"code": "subscription_not_found"})
        if request.url.path.endswith("/confirmations"):
            return httpx.Response(403, json={"code": "user_token_app_denied"})
        return httpx.Response(200, json={"subscription_id": "s-1", "revision": 2})

    client = HttpKnowbaseUser(
        environ={"KNOWBASE_INTERNAL_URL": "https://knowbase.internal"},
        transport=httpx.MockTransport(handler),
    )
    await client.subscribe("user-token", "s-1", 2, {"by": "url"})
    # 已经撤了的再撤，算撤了
    await client.unsubscribe("user-token", "s-1", 2)
    with pytest.raises(SupplyError) as caught:
        await client.confirm("user-token", kind="url", items=[{"url": PAGE}], supply={})
    # knowbase 还没放开确认接口给 knowfeed：照实转
    assert (caught.value.status, caught.value.code) == (403, "user_token_app_denied")
    assert [(r.method, r.url.path) for r in seen] == [
        ("PUT", "/api/internal/v2/user-data/subscriptions/s-1/revisions/2"),
        ("DELETE", "/api/internal/v2/user-data/subscriptions/s-1/revisions/2"),
        ("POST", "/api/internal/v2/user-data/confirmations"),
    ]
    assert all(r.headers["authorization"] == "Bearer user-token" for r in seen)
    with pytest.raises(SupplyError) as caught:
        await HttpKnowbaseUser(environ={}).confirm("t", kind="url", items=[], supply={})
    assert caught.value.code == "knowbase_unconfigured"


class Tokens:
    async def get_token(self) -> str:
        return "service-token"


async def test_knowbase_supply_checks_a_subscription_and_sends_a_collected_original():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/collected-originals"):
            return httpx.Response(
                201,
                json=[
                    {
                        "subject": "alice",
                        "item_ref": "demand:d:c1",
                        "material_id": "m-1",
                        "version_no": 1,
                        "created": True,
                    }
                ],
            )
        if "subscriptions" in request.url.path:
            if request.url.path.endswith("/revisions/1"):
                return httpx.Response(404, json={"code": "subscription_not_found"})
            return httpx.Response(200, json={"subscription_id": "s-1", "revision": 2})
        return httpx.Response(
            201, json={"material_id": "m-1", "version_no": 1, "created": True}
        )

    kb = HttpKnowbase(
        base_url="https://knowbase.internal/api/internal/v2/user-data",
        tokens=Tokens(),  # type: ignore[arg-type]
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )
    assert await kb.check_subscription("s-1", 1, "alice") is None
    assert (await kb.check_subscription("s-1", 2, "alice"))["revision"] == 2  # type: ignore[index]
    assert seen[-1].url.params["subject"] == "alice"
    # 为用户采集：查询参数写渠道、许可档，multipart 里是原件与各受益人（各自的预填）
    answered = await kb.deliver_collected(
        channel="site-www-stats-gov-cn",
        channel_doc_id="url:abc",
        name="公报.html",
        content=b"<p>x</p>",
        license="self",
        source_url=PAGE,
        beneficiaries=[
            {
                "subject": "alice",
                "prefill": {"title": "公报"},
                "item_ref": "demand:d:c1",
            }
        ],
    )
    sent = seen[-1]
    params = sent.url.params
    assert sent.url.path.endswith("/service/collected-originals")
    assert params["license"] == "self" and params["name"].endswith(".html")
    body = sent.content
    assert b'name="file"' in body and b"<p>x</p>" in body
    assert b'name="beneficiaries"' in body and "公报".encode() in body
    assert answered[0]["material_id"] == "m-1"


async def test_page_work_runs_off_the_event_loop():
    """吃 CPU 的加工放线程里（独立审第 5 条）：按页文字不在事件循环的线程里跑。"""
    import threading

    from app.application.supply.jobs import JobRunner

    threads: list[int] = []

    class Pages:
        def text_pages(self, pdf):
            threads.append(threading.get_ident())
            return [(1, "x")]

    class Knowbase:
        async def write_pages(self, grant, version, pages):
            return {}

    runner = JobRunner(
        None, lambda c: None, Knowbase(), None, pdf_pages=Pages(), pdf_words=None
    )  # type: ignore[arg-type]
    assert await runner._write_pages("g", "m", b"%PDF-1.7") == [
        {"page": 1, "text": "x"}
    ]
    assert threads and threads[0] != threading.get_ident()


async def test_collected_originals_wait_for_knowbase_and_never_go_public():
    """knowbase 的「为某用户采集」路还没上（404、405）：抛 awaiting_knowbase，调用方不交付
    （二审第 1 条），绝不改走公开原件层。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"detail": "Not Found"})

    kb = HttpKnowbase(
        base_url="https://knowbase.internal/api/internal/v2/user-data",
        tokens=Tokens(),  # type: ignore[arg-type]
        timeout_seconds=5,
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(SupplyError) as caught:
        await kb.deliver_collected(
            channel="c",
            channel_doc_id="url:x",
            name="a.html",
            content=b"x",
            license="self",
            source_url=PAGE,
            beneficiaries=[],
        )
    assert (caught.value.status, caught.value.code) == (409, "awaiting_knowbase")


def test_urls_the_crawler_will_not_fetch_are_refused_up_front():
    """网址本身的毛病受理时就拒（二审第 3 条）：非默认端口、带口令、IP 地址、别的协议。"""
    from app.domain.supply.catalog import site_of

    for bad in (
        "https://www.stats.gov.cn:8443/a",
        "https://user:pw@www.stats.gov.cn/a",
        "http://10.0.0.1/a",
        "ftp://www.stats.gov.cn/a",
    ):
        with pytest.raises(SupplyError) as caught:
            site_of(bad)
        assert caught.value.code == "url_invalid"
    assert site_of("https://www.stats.gov.cn:443/a") == "www.stats.gov.cn"


async def test_page_fetch_problems_with_the_url_are_not_channel_failures(monkeypatch):
    """取的时候解析不了、地址被禁、跳出网站的，是「这一条不行」（422），不算渠道的错；
    跳到别的网站的那一跳不发（二审第 3、16 条）。"""
    from app.application.ports.securities import FetchFailed
    from app.infrastructure.supply.adapters import CrawlHttp

    hops: list[bool] = []

    class Fetcher:
        def __init__(self, code):
            self.code = code

        async def fetch(self, request, *, allow_redirect=None):
            if allow_redirect is not None:
                hops.append(allow_redirect("https://evil.example.com/x"))
                hops.append(allow_redirect("https://data.stats.gov.cn/x"))
            raise FetchFailed(self.code)

    for code, expected in (
        ("crawl_dns_invalid", (422, "url_rejected")),
        ("crawl_dns_failed", (503, "fetch_failed:crawl_dns_failed")),
        ("crawl_address_forbidden", (422, "url_rejected")),
        ("crawl_redirect_off_site", (422, "off_site")),
        ("crawl_timeout", (503, "fetch_failed:crawl_timeout")),
    ):
        with pytest.raises(SupplyError) as caught:
            await CrawlHttp(Fetcher(code)).page(  # type: ignore[arg-type]
                PAGE, max_bytes=1, timeout_seconds=1, fetch=True, site="stats.gov.cn"
            )
        assert (caught.value.status, caught.value.code) == expected
    assert hops[:2] == [False, True]


async def test_the_crawler_stops_before_a_redirect_off_the_site():
    """取数边界在发出跳转之前先问一遍：跳到别的网站的那一跳不发。"""
    from app.infrastructure.external import crawl_http

    sent: list[str] = []

    class Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def stream(self, method, url, **kw):
            sent.append(kw["extensions"]["sni_hostname"])
            return Response()

    class Response:
        status_code = 302
        headers = {"location": "https://evil.example.com/next"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    async def target(url):
        return "93.184.216.34"

    import pytest as _pytest

    mp = _pytest.MonkeyPatch()
    mp.setattr(crawl_http, "_target", target)
    mp.setattr(crawl_http.httpx, "AsyncClient", Client)
    try:
        with pytest.raises(crawl_http.CrawlFetchError) as caught:
            await crawl_http.fetch_crawl_url(
                "https://www.stats.gov.cn/a",
                timeout_seconds=5,
                max_bytes=100,
                allow_redirect=lambda u: "stats.gov.cn" in u,
            )
    finally:
        mp.undo()
    assert str(caught.value) == "crawl_redirect_off_site"
    assert sent == ["www.stats.gov.cn"]  # 别站那一跳没发
