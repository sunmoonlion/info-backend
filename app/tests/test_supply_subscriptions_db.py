"""订阅与按主题找（SDD 0045 第 2.2、2.5 节，第四节；发版前补齐 F1、F4）：建订阅凭本人令牌在
knowbase 留范围同意；每轮先核同意还在、看新的、只收起始日之后的、每月上限与下月补取、到期与
撤销；按网址订订阅源；按主题找与按主题订阅的候选勾选。走真数据库，渠道、knowbase 换成假的。"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

from test_durable_delivery_db import db as db
from test_durable_delivery_db import sql
from test_supply_catalog_db import REG
from test_supply_db import refused
from test_supply_web_db import ORG, WebWorld, html

from app.application.ports.supply import Page
from app.application.supply.searches import SearchRunner, SearchService
from app.application.supply.subscriptions import SubscriptionService, WatchRunner
from app.domain.supply.models import Entry
from app.domain.supply.quota import Quota
from app.infrastructure.repositories.supply_subscription import (
    SqlSubscriptionRepository,
)

TARGET = "000858.SZ|interim_announcement"


class FakeUser:
    """凭本人令牌调 knowbase 的那一头：建、续、撤范围同意，记确认。"""

    def __init__(self, knowbase) -> None:
        self.knowbase = knowbase
        self.calls: list[tuple] = []

    async def subscribe(self, token, subscription_id, revision, scope):
        self.calls.append(("subscribe", token, subscription_id, revision, scope))
        self.knowbase.subscriptions[(subscription_id, revision)] = scope
        return {"subscription_id": subscription_id, "revision": revision}

    async def unsubscribe(self, token, subscription_id, revision):
        self.calls.append(("unsubscribe", subscription_id, revision))
        self.knowbase.subscriptions.pop((subscription_id, revision), None)

    async def confirm(self, token, *, kind, items, supply):
        self.calls.append(("confirm", kind, items))
        number = len(self.knowbase.confirmations) + 1
        confirmation = f"conf-user-{number}"
        made = [{"item_id": f"c{n + 1}", **i} for n, i in enumerate(items)]
        self.knowbase.confirmations[confirmation] = {
            "subject": "alice",
            "items": made,
            "supply": supply,
        }
        return {
            "confirmation_id": confirmation,
            "items": made,
            "branches": [{"item_id": i["item_id"], "branch": "supply"} for i in made],
        }


class WindowCatalog:
    """巨潮的「列」：按披露时间窗回这个窗里的公告。"""

    def __init__(self) -> None:
        self.notices: list[Entry] = []

    def add(self, number: int, day: date) -> None:
        self.notices.append(
            Entry(
                "cninfo",
                f"000858:notice:{number}",
                f"五粮液公告{number}",
                f"http://static.cninfo.com.cn/finalpage/{day}/{number}.PDF",
                day,
                announcement_id=str(number),
                doc_type="interim_announcement",
            )
        )

    async def list(self, key):
        return [
            e
            for e in self.notices
            if key.period_start <= e.disclosed_on <= key.period_end  # type: ignore[operator]
        ]

    async def fetch(self, entry):
        return b"%PDF-1.7 " + entry.channel_doc_id.encode(), "application/pdf"


class Subs:
    def __init__(self, db) -> None:
        self.w = w = WebWorld(db)
        self.repo = SqlSubscriptionRepository(lambda: db)
        self.user = FakeUser(w.knowbase)
        self.searches = SearchService(
            self.repo, w.repo, w.demands, w.knowbase, self.user, clock=w.clock
        )
        self.service = SubscriptionService(
            self.repo,
            w.repo,
            w.desk.repo,
            w.knowbase,
            self.user,
            self.searches,
            quota=Quota(subscriptions=2, monthly_items=50),
            clock=w.clock,
        )
        w.runner.watches = WatchRunner(self.repo, clock=w.clock)
        w.runner.searches = SearchRunner(self.repo, w.desk.repo, clock=w.clock)
        self.catalog = WindowCatalog()
        w.catalogs["cninfo"] = self.catalog  # type: ignore[assignment]

    @property
    def today(self) -> date:
        return (self.w.clock.now + timedelta(hours=8)).date()

    async def run_all(self) -> list[str]:
        """调度一轮轮排、跑完排出去的作业，直到排不出来；回各作业的结果。"""
        out = []
        while picked := await self.w.dispatch():
            for job in picked:
                out.append(await self.w.runner.run(job))
        return out

    async def create(self, **kw):
        base = dict(
            by="description",
            subjects=("000858.SZ",),
            doc_types=("interim_announcement",),
            readings=("original",),
            ends_on=self.today + timedelta(days=200),
            every_hours=24,
            monthly_cap=10,
            purpose="跟踪五粮液公告",
        )
        base.update(kw)
        return await self.service.create("user-token", "alice", **base)  # type: ignore[arg-type]


async def test_a_description_subscription_runs_rounds_from_its_own_progress(db):
    s = Subs(db)
    s.catalog.add(1, s.today - timedelta(days=3))  # 订阅之前就有的
    sub = await s.create(monthly_cap=2)
    # 凭本人令牌在 knowbase 留了长期范围同意
    assert s.user.calls[0][:4] == ("subscribe", "user-token", sub.id, 1)
    assert s.user.calls[0][4]["subjects"] == ["000858.SZ"]
    assert sub.channels == {TARGET: "cninfo"}
    # 第一轮：排一个「看新的」作业（第 ③ 档），跑完收新发现
    assert (await s.service.tick())["started"] == 1
    assert await sql(db, "SELECT tier FROM supply_job WHERE kind = 'watch'") == 3
    assert await s.run_all() == ["done"]
    assert (await s.service.tick())["finished"] == 1
    _, rounds = await s.service.get("alice", sub.id)
    # 订阅之前就披露的不算新的
    assert (rounds[0].found, rounds[0].taken) == (0, 0)

    # 第二天：三份新公告，本月上限 2 份
    s.w.clock.now += timedelta(days=1)
    for n in (2, 3, 4):
        s.catalog.add(n, s.today)
    assert (await s.service.tick())["started"] == 1
    assert await s.run_all() == ["done"]
    await s.service.tick()
    _, rounds = await s.service.get("alice", sub.id)
    assert (rounds[0].found, rounds[0].taken, rounds[0].over_cap) == (3, 2, 1)
    demand_id = rounds[0].demand_id
    assert demand_id is not None
    demand = await s.w.repo.get_demand(demand_id)
    assert (demand.proposer, demand.confirmation_id) == (  # type: ignore[union-attr]
        "subscription",
        f"subscription:{sub.id}@1",
    )
    items = await s.w.repo.items_of(demand_id)
    assert [i.status for i in items] == ["fetching", "fetching"]
    # 条目已知，作业不再「列」：直接取、交付
    assert await s.run_all() == ["done", "done"]
    items = await s.w.repo.items_of(demand_id)
    assert [i.status for i in items] == ["ready", "ready"]

    # 下个月初：上个月超额没取的先补取
    s.w.clock.now = s.w.clock.now.replace(day=1) + timedelta(days=40)
    s.w.clock.now = s.w.clock.now.replace(day=2)
    await s.service.tick()
    await s.run_all()
    await s.service.tick()
    _, rounds = await s.service.get("alice", sub.id)
    assert rounds[0].taken == 1
    assert (
        await sql(
            db,
            "SELECT count(*) FROM supply_subscription_take WHERE status = 'over_cap'",
        )
        == 0
    )


async def test_withdrawn_consent_and_expiry_end_a_subscription(db):
    s = Subs(db)
    sub = await s.create()
    await s.service.tick()
    await s.run_all()
    await s.service.tick()
    s.w.clock.now += timedelta(days=1)
    s.catalog.add(7, s.today)
    await s.service.tick()
    await s.run_all()
    await s.service.tick()
    # 本人在 knowbase 撤了范围同意：下一轮不再生成新需求，在途项取消
    del s.w.knowbase.subscriptions[(sub.id, 1)]
    s.w.clock.now += timedelta(days=1)
    assert (await s.service.tick())["ended"] == 1
    sub, _ = await s.service.get("alice", sub.id)
    assert (sub.status, sub.status_reason) == ("disabled", "consent_withdrawn")
    statuses = await sql(
        db,
        "SELECT string_agg(status || ':' || coalesce(reason, ''), ',') FROM supply_demand_item",
    )
    assert statuses == "cancelled:subscription_ended"

    # 到期不续：转「已到期」
    other = await s.create(ends_on=s.today + timedelta(days=10))
    assert other.renew_due(s.today)  # 离截止日 14 天以内：提醒续订
    s.w.clock.now += timedelta(days=11)
    await s.service.tick()
    other, _ = await s.service.get("alice", other.id)
    assert other.status == "expired"
    # 续订 = 范围同意出新修订
    renewed = await s.service.renew(
        "user-token", "alice", other.id, s.today + timedelta(days=100)
    )
    assert (renewed.revision, renewed.status) == (2, "active")
    assert (other.id, 2) in s.w.knowbase.subscriptions


async def test_pause_resume_cancel_and_limits(db):
    s = Subs(db)
    sub = await s.create()
    paused = await s.service.pause("alice", sub.id)
    assert paused.status == "paused"
    assert (await s.service.tick()).get("paused") == 1
    assert await sql(db, "SELECT count(*) FROM supply_job") == 0
    await s.service.resume("alice", sub.id)
    await s.create(subjects=("600519.SH",))
    # 订阅条数到了配额
    await refused(s.create(subjects=("000001.SZ",)), 429, "quota_subscriptions")
    cancelled = await s.service.cancel("user-token", "alice", sub.id)
    assert cancelled.status == "cancelled"
    assert ("unsubscribe", sub.id, 1) in s.user.calls
    # 没有能「看新的」的渠道，订阅建不起来
    await refused(
        s.create(doc_types=("statistical_bulletin",), subjects=(ORG,)),
        422,
        "no_watch_channel",
    )
    await refused(
        s.create(
            by="url",
            urls=("https://www.example.com/rss.xml",),
            subjects=(),
            doc_types=(),
        ),
        422,
        "no_watch_channel",
    )
    await refused(s.service.get("bob", sub.id), 404, "subscription_not_found")


FEED = "https://data.stats.gov.cn/rss.xml"


async def test_a_url_subscription_watches_a_feed(db):
    s = Subs(db)
    await s.w.desk.catalog.register(
        "stats-feed",
        replace(
            REG,
            name="统计局数据发布",
            kind="feed",
            plugin="feed",
            domain="data.stats.gov.cn",
            doc_types=("statistical_data",),
            readings=("original", "text"),
            config={"subject": ORG},
            obey_robots=False,
        ),
        actor="op",
    )
    day = s.today

    def feed(*numbers: int) -> Page:
        items = "".join(
            f"<item><title>数据{n}</title><link>https://data.stats.gov.cn/n/{n}.html</link>"
            f"<pubDate>{day.strftime('%a, %d %b %Y')} 08:00:00 +0800</pubDate></item>"
            for n in numbers
        )
        return Page(
            200,
            "application/rss+xml",
            f"<rss><channel>{items}</channel></rss>".encode(),
            FEED,
        )

    s.w.http.pages = {FEED: feed(1)}
    for n in (1, 2):
        url = f"https://data.stats.gov.cn/n/{n}.html"
        s.w.http.pages[url] = html(
            f"<html><head><title>数据{n}</title></head><body><article><p>"
            + "国家统计局发布的月度数据，工业增加值同比增长。" * 5
            + "</p></article></body></html>",
            url,
        )
    trial = await s.w.desk.catalog.request_trial("stats-feed", actor="op", url=FEED)
    assert await s.w.desk.trials.run(trial.id) == "ok"
    # 统计局官方发布：审核定为法定披露区，描述按渠道配置（主体是统计局的机构编号）
    await s.w.desk.catalog.review(
        "stats-feed", "approve", actor="op", reason="官方", zone="disclosure"
    )
    sub = await s.create(
        by="url", urls=(FEED,), subjects=(), doc_types=(), readings=("original", "text")
    )
    assert sub.channels == {FEED: "stats-feed"}
    await s.service.tick()
    await s.run_all()
    await s.service.tick()
    _, rounds = await s.service.get("alice", sub.id)
    assert rounds[0].taken == 1
    assert await s.run_all() == ["done"]
    items = await s.w.repo.items_of(str(rounds[0].demand_id))
    assert (items[0].url, items[0].status) == (
        "https://data.stats.gov.cn/n/1.html",
        "ready",
    )
    assert s.w.knowbase.delivered[-1]["subject"] == ORG


SEARCH = "https://www.stats.gov.cn/search?q=%E5%B7%A5%E4%B8%9A"


async def _search_channel(s: Subs) -> None:
    await s.w.desk.catalog.register(
        "stats-search",
        replace(
            REG,
            name="统计局站内搜索",
            kind="search",
            plugin="site-search",
            domain="www.stats.gov.cn",
            doc_types=("statistical_bulletin",),
            readings=(),
            config={
                "search_url": "https://www.stats.gov.cn/search?q={q}",
                "result_pattern": "/r/",
            },
            obey_robots=False,
        ),
        actor="op",
    )
    s.w.http.pages[SEARCH] = html(
        '<a href="/r/2.html">工业二</a><a href="/r/1.html">工业一</a>', SEARCH
    )
    trial = await s.w.desk.catalog.request_trial(
        "stats-search", actor="op", topic="工业"
    )
    assert await s.w.desk.trials.run(trial.id) == "ok"
    await s.w.desk.catalog.review("stats-search", "approve", actor="op", reason="官方")


async def test_topic_search_lists_candidates_and_picks_become_url_items(db):
    s = Subs(db)
    await _search_channel(s)
    nothing = await s.searches.start("alice", "工业", ("www.example.com",), 5)
    assert (nothing.status, nothing.note) == ("ended", "no_search_channel")
    search = await s.searches.start("alice", "工业", (), 5)
    assert search.status == "searching"
    assert await s.run_all() == ["done"]
    search = await s.searches.get("alice", search.id)
    assert search.status == "ready"
    assert search.expires_at == s.w.clock.now + timedelta(days=7)
    # 原始顺序；每条先预切合：统计局网站还没登记（站内搜索渠道是 www 子域名那条）
    assert [c["url"] for c in search.candidates] == [
        "https://www.stats.gov.cn/r/2.html",
        "https://www.stats.gov.cn/r/1.html",
    ]
    assert search.candidates[0]["site"] == "approved"
    demand, items, _ = await s.searches.pick(
        "user-token",
        "alice",
        search.id,
        ["k1"],
        readings=("original",),
        idempotency_key="idem-pick-0001",
        deadline=s.w.clock.now + timedelta(days=3),
    )
    # 本人确认一次（kind=topic，带候选编号），勾的变成给网址的需求项
    confirm = next(c for c in s.user.calls if c[0] == "confirm")
    assert confirm[1] == "topic" and confirm[2][0]["candidate_ref"] == f"{search.id}:k1"
    assert (items[0].url, items[0].channel_id) == (
        "https://www.stats.gov.cn/r/2.html",
        "stats-search",
    )
    assert (await s.searches.get("alice", search.id)).status == "ended"
    # 7 天不勾作废
    other = await s.searches.start("alice", "工业", (), 5)
    await s.run_all()
    s.w.clock.now += timedelta(days=8)
    assert (await s.searches.get("alice", other.id)).status == "ended"


async def test_a_topic_subscription_searches_each_round_and_picks_under_its_consent(db):
    s = Subs(db)
    await _search_channel(s)
    sub = await s.create(by="topic", topic="工业", subjects=(), doc_types=())
    await s.service.tick()
    sub, _ = await s.service.get("alice", sub.id)
    search_id = sub.channels["search"]
    await s.run_all()
    await s.service.tick()
    _, rounds = await s.service.get("alice", sub.id)
    assert (rounds[0].found, rounds[0].note) == (2, f"search:{search_id}")
    before = len([c for c in s.user.calls if c[0] == "confirm"])
    demand, items, _ = await s.searches.pick(
        None,
        "alice",
        search_id,
        ["k2"],
        readings=("original",),
        idempotency_key="idem-pick-sub-1",
        subscriptions=s.service,
    )
    # 凭订阅的范围同意，不另记确认
    assert len([c for c in s.user.calls if c[0] == "confirm"]) == before
    assert (demand.proposer, demand.confirmation_id) == ("subscription", sub.consent)
    assert items[0].url == "https://www.stats.gov.cn/r/1.html"
