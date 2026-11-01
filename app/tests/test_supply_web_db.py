"""给网址的需求（SDD 0045 第 2.2 节、第四节「给网址」）与独立审在 2359a81、21a45ea 里查出的
问题的复现。走真数据库，网站、knowbase 换成假的。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import timedelta

from test_durable_delivery_db import db as db
from test_durable_delivery_db import sql
from test_supply_catalog_db import REG, Desk
from test_supply_db import KEY, PDF, FakeCatalog, FakeKnowbase, World, refused
from test_supply_schedule_db import FakeReader, job_of, submit, year

from app.application.ports.supply import Page
from app.application.supply.scheduler import ProcessingIntake
from app.application.supply.web import WebSite
from app.domain.supply.catalog import Registration, web_doc_id
from app.domain.supply.models import DemandItemIn, DescriptionKey, SupplyError
from app.domain.supply.schedule import PROCESS_QUEUE
from app.infrastructure.repositories.supply import SqlSupplyRepository

PAGE = "https://www.stats.gov.cn/sj/zxfb/202610/t1.html"
SITE = "site-www-stats-gov-cn"
ORG = "org:11100000000013127N"


class StableKnowbase(FakeKnowbase):
    """同一渠道同一编号认作同一份资料（knowbase 的真行为）：第二次交回 created=False。"""

    def __init__(self) -> None:
        super().__init__()
        self.materials: dict[tuple[str, str], str] = {}
        self.subscriptions: dict[tuple[str, int], dict] = {}

    async def deliver_public(self, **kw):
        if self.fail_deliver:
            raise self.fail_deliver
        self.calls.append(("deliver", kw["channel_doc_id"]))
        self.delivered.append(kw)
        key = (kw["channel"], kw["channel_doc_id"])
        created = key not in self.materials
        self.materials.setdefault(key, f"m-{len(self.materials) + 1}")
        return {"material_id": self.materials[key], "version_no": 1, "created": created}

    async def check_subscription(self, subscription_id, revision, subject):
        return self.subscriptions.get((subscription_id, revision))


class FakeSiteHttp:
    def __init__(self, pages: dict[str, Page]) -> None:
        self.pages = pages
        self.asked: list[str] = []

    async def page(self, url, *, max_bytes, timeout_seconds, fetch, site=None):
        self.asked.append(url)
        return self.pages.get(url, Page(404, "text/html", b"", url))


def html(body: str, url: str) -> Page:
    return Page(200, "text/html; charset=utf-8", body.encode(), url)


class WebWorld(World):
    """需求那一头接上按网站审（`ensure_site`），渠道插件里通用网页、订阅源、站内搜索走
    `WebSite`（取数换成假的网站）。"""

    def __init__(self, db) -> None:
        super().__init__(db)
        self.knowbase = StableKnowbase()
        self.demands.knowbase = self.knowbase
        self.runner.knowbase = self.knowbase
        self.desk = Desk(db, self)
        self.demands.sites = self.desk.catalog.ensure_site
        self.http = FakeSiteHttp({})
        self.reader = FakeReader({})
        self.runner.reader = self.reader
        self.runner.plugins = self.plugin
        self.desk.trials.plugins = self.plugin

    def plugin(self, channel):
        if channel.plugin in ("web", "feed", "site-search"):
            return WebSite(self.http, channel)
        return self.catalogs.get(channel.plugin) or self.desk.catalogs.get(
            channel.plugin
        )

    def confirm_urls(
        self, confirmation_id: str, subject: str, *urls: str, follow: bool = False
    ):
        self.knowbase.confirmations[confirmation_id] = {
            "subject": subject,
            "items": [
                {"item_id": f"c{n + 1}", "url": u, "follow_links": follow}
                for n, u in enumerate(urls)
            ],
            "supply": {},
        }

    async def submit_url(
        self,
        owner: str,
        url: str,
        *,
        follow=False,
        n=1,
        readings=("original", "text"),
        supply=None,
    ):
        confirmation = f"conf-url-{owner}-{n}"
        self.confirm_urls(confirmation, owner, url, follow=follow)
        if supply is not None:
            self.knowbase.confirmations[confirmation]["supply"] = supply
        return await self.demands.submit(
            app="knowbench",
            owner=owner,
            proposer="app",
            chain=(),
            task_ref=None,
            confirmation_id=confirmation,
            idempotency_key=f"idem-url-{owner}-{n}",
            items=[
                DemandItemIn(
                    None,
                    readings,
                    "c1",
                    url=url,
                    follow_links=follow,
                    prefill={"doc_type": "statistical_bulletin", "subject": ORG},
                )
            ],
        )

    async def approve_site(self, url: str, license="via_expert") -> None:
        site = await self.desk.catalog.ensure_site(url)
        trial = await self.desk.catalog.request_trial(site.id, actor="op", url=url)
        assert await self.desk.trials.run(trial.id) == "ok"
        await self.desk.catalog.review(
            site.id, "approve", actor="op", reason="统计局官网", license=license
        )


async def test_a_url_on_a_new_site_waits_for_review_then_is_fetched(db):
    w = WebWorld(db)
    w.http.pages = {
        PAGE: html("<html><title>公报</title><p>工业增加值增长。</p></html>", PAGE)
    }
    demand, items, _ = await w.submit_url("alice", PAGE)
    # 没登记的网站：自动建一条待审渠道，需求项停在「已受理」附「网站待审」
    assert (items[0].status, items[0].reason, items[0].channel_id) == (
        "accepted",
        "site_pending",
        SITE,
    )
    assert items[0].url == PAGE and items[0].key is None
    assert (
        await sql(db, "SELECT review FROM supply_channel WHERE id = :i", i=SITE)
        == "pending"
    )
    # 平台人员试采、放行：在等的需求项当场挂上作业
    await w.approve_site(PAGE, license="self")
    _, items = await w.demands.get("knowbench", demand.id)
    assert items[0].status == "fetching"
    # 第二个人要同一网址：共享这个作业
    _, other, _ = await w.submit_url("bob", PAGE)
    assert other[0].status == "fetching"
    job = await job_of(db, items[0].id)
    assert await job_of(db, other[0].id) == job
    assert await w.runner.run(job) == "done"
    _, items = await w.demands.get("knowbench", demand.id)
    assert (items[0].status, items[0].receipt["readings"]) == (
        "ready",
        ["original", "text"],
    )
    sent = w.knowbase.delivered[-1]
    # 网站默认是「为用户采集」：走 knowbase 那条路（二审第 1 条），不进公开原件层；许可按
    # 审核定的；每人的预填只用在自己那份
    assert ("collected", web_doc_id(PAGE)) in w.knowbase.calls
    assert not any(c[0] == "deliver" for c in w.knowbase.calls)
    assert sent["name"].endswith(".html") and sent["license"] == "self"
    assert [(b["subject"], b["item_ref"]) for b in sent["beneficiaries"]] == [
        ("alice", items[0].basis_ref),
        ("bob", f"demand:{other[0].demand_id}:c1"),
    ]
    assert sent["beneficiaries"][0]["prefill"] == {
        "doc_type": "statistical_bulletin",
        "subject": ORG,
    }
    # 各人拿到的是自己名下那一份；不记公开原件层的引用
    _, bobs = await w.demands.get("knowbench", other[0].demand_id)
    assert items[0].receipt["material_id"] != bobs[0].receipt["material_id"]
    assert not any(c[0] == "references" for c in w.knowbase.calls)
    # 加工一次，用第一位受益人那份的授权
    assert ("grant", "alice") in w.knowbase.calls
    # 加工授权用完交还（独立审第 11 条）
    assert w.reader.released == ["g-1"]


async def test_follow_links_takes_at_most_twenty_files_and_reports_the_rest(db):
    w = WebWorld(db)
    links = "".join(f'<a href="/files/{n}.pdf">附件{n}</a>' for n in range(22))
    pages = {PAGE: html(links, PAGE)}
    for n in range(22):
        url = f"https://www.stats.gov.cn/files/{n}.pdf"
        if n != 3:  # 第 4 个链接对方回 404
            pages[url] = Page(200, "application/pdf", PDF + str(n).encode(), url)
    w.http.pages = pages
    await w.approve_site(PAGE)
    demand, items, _ = await w.submit_url("alice", PAGE, follow=True)
    assert await w.runner.run(await job_of(db, items[0].id)) == "done"
    _, items = await w.demands.get("knowbench", demand.id)
    receipt = items[0].receipt
    assert receipt["not_taken"] == 2
    assert len(receipt["materials"]) == 20  # 这一页 + 19 份（20 份里一份没取到）
    assert receipt["failed_links"] == [
        {"url": "https://www.stats.gov.cn/files/3.pdf", "reason": "page_not_found"}
    ]
    names = [d["name"] for d in w.knowbase.delivered]
    assert names[0].endswith(".html") and names[1].endswith(".pdf")


async def test_rejected_sites_and_unmet_conditions_end_url_items(db):
    w = WebWorld(db)
    site = await w.desk.catalog.ensure_site(PAGE)
    await w.desk.catalog.review(site.id, "reject", actor="op", reason="不让采")
    _, items, _ = await w.submit_url("alice", PAGE)
    assert (items[0].status, items[0].reason) == ("unsatisfiable", "site_rejected")
    # 待审期间提的、要「可以出售」的：网站审过定了「只许本人用」，转「当前供给条件不满足」
    other = "https://www.mof.gov.cn/a.html"
    w.http.pages = {other: html("<p>财政</p>", other)}
    demand, items, _ = await w.submit_url(
        "bob", other, supply={"conditions": {"min_license": "sale"}}
    )
    assert items[0].reason == "site_pending"
    await w.approve_site(other, license="self")
    _, items = await w.demands.get("knowbench", demand.id)
    assert (items[0].status, items[0].reason) == ("unsatisfiable", "conditions_unmet")


async def test_org_subjects_are_accepted_for_policy_and_statistics(db):
    w = WebWorld(db)
    key = DescriptionKey(ORG, "statistical_bulletin")
    w.confirm("conf-org", "alice", key)
    _, items, _ = await w.demands.submit(
        app="knowbench",
        owner="alice",
        proposer="app",
        chain=(),
        task_ref=None,
        confirmation_id="conf-org",
        idempotency_key="idem-org-0001",
        items=[DemandItemIn(key, ("original",), "c1")],
    )
    # 格式认了；来源目录里还没有统计公报的渠道：进缺口清单
    assert (items[0].status, items[0].reason) == ("unsatisfiable", "no_channel")
    bad = DescriptionKey(
        ORG, "annual_report", year(2025).period_start, year(2025).period_end
    )
    w.confirm("conf-bad", "alice", bad)
    await refused(
        w.demands.submit(
            app="knowbench",
            owner="alice",
            proposer="app",
            chain=(),
            task_ref=None,
            confirmation_id="conf-bad",
            idempotency_key="idem-org-0002",
            items=[DemandItemIn(bad, ("original",), "c1")],
        ),
        422,
        "subject_type_mismatch",
    )


async def test_deadlines_turn_open_items_into_timeouts(db):
    w = WebWorld(db)
    w.confirm("conf-d", "alice", KEY)
    demand, items, _ = await w.demands.submit(
        app="knowbench",
        owner="alice",
        proposer="app",
        chain=(),
        task_ref=None,
        confirmation_id="conf-d",
        idempotency_key="idem-deadline-1",
        items=[DemandItemIn(KEY, ("original",), "c1")],
        deadline=w.clock.now + timedelta(hours=2),
        note="下周要用",
    )
    assert (demand.deadline, demand.note) == (
        w.clock.now + timedelta(hours=2),
        "下周要用",
    )
    repo = SqlSupplyRepository(lambda: db)
    assert await repo.expire_deadlines(w.clock.now) == 0
    assert await repo.expire_deadlines(w.clock.now + timedelta(hours=3)) == 1
    _, items = await w.demands.get("knowbench", demand.id)
    assert (items[0].status, items[0].reason) == ("timeout", "deadline_passed")


# —— 独立审 ——


async def test_review_1_a_redelivered_version_reports_what_was_really_done(db):
    """knowbase 回 created=False 时，读法以原件记录里记的为准；没有记录就重新加工。"""
    w = WebWorld(db)
    _, items = await submit(w, "alice", KEY)
    assert await w.runner.run(await job_of(db, items[0].id)) == "done"
    # 当时抽表照实失败了（记下来的只有原件与按页文字）
    await sql(
        db,
        """UPDATE supply_original_delivery SET readings = '["original", "pages"]'""",
    )
    grants = sum(1 for c in w.knowbase.calls if c[0] == "grant")
    demand, items = await submit(w, "bob", KEY, n=2)
    w.confirm("conf-bob-tables", "bob", KEY)
    _, tables, _ = await w.demands.submit(
        app="knowbench",
        owner="bob",
        proposer="app",
        chain=(),
        task_ref=None,
        confirmation_id="conf-bob-tables",
        idempotency_key="idem-bob-tables",
        items=[DemandItemIn(KEY, ("original", "table"), "c1")],
    )
    assert await w.runner.run(await job_of(db, items[0].id)) == "done"
    _, done = await w.demands.get("knowbench", tables[0].demand_id)
    # 要表的不报「已就绪」：表没做成
    assert (done[0].status, done[0].reason) == ("partial", "readings_incomplete")
    assert sum(1 for c in w.knowbase.calls if c[0] == "grant") == grants  # 没重做
    # 原件记录里没记读法：重新领授权加工
    await sql(db, "UPDATE supply_original_delivery SET readings = '[]'")
    _, again = await submit(w, "carol", KEY, n=3)
    assert await w.runner.run(await job_of(db, again[0].id)) == "done"
    assert sum(1 for c in w.knowbase.calls if c[0] == "grant") == grants + 1


class AttachingCatalog(FakeCatalog):
    """取的中途又有人提了同一份（作业在跑时挂上来的需求项）。"""

    def __init__(self, attach) -> None:
        super().__init__()
        self.attach = attach

    async def fetch(self, entry):
        if self.attach is not None:
            work, self.attach = self.attach, None
            await work()
        return await super().fetch(entry)


async def test_review_2_items_attached_while_running_are_not_left_fetching(db):
    w = WebWorld(db)
    late: dict = {}

    async def attach():
        late["made"] = await submit(w, "bob", KEY)

    w.catalogs["cninfo"] = AttachingCatalog(attach)
    _, items = await submit(w, "alice", KEY)
    job = await job_of(db, items[0].id)
    assert await w.runner.run(job) == "done"
    _, bob = late["made"]
    assert await job_of(db, bob[0].id) == job
    # 作业放回队列再跑一次，不是「已办完」
    assert (
        await sql(db, "SELECT status FROM supply_job WHERE id = :j", j=job) == "queued"
    )
    assert await w.runner.run(job) == "done"
    _, bob = await w.demands.get("knowbench", bob[0].demand_id)
    assert bob[0].status == "ready"
    assert await sql(db, "SELECT status FROM supply_job WHERE id = :j", j=job) == "done"


class DownCatalog(FakeCatalog):
    async def list(self, key):
        raise SupplyError(404, "security_not_found_at_source", "not on cninfo")


async def test_review_3_four_hundreds_do_not_pause_the_channel(db):
    w = WebWorld(db)
    w.catalogs["cninfo"] = DownCatalog()
    for n in range(6):
        _, items = await submit(w, f"user-{n}", year(2020 + n))
        assert await w.runner.run(await job_of(db, items[0].id)) == "absent"
    status, failures = (
        await sql(db, "SELECT status FROM supply_channel WHERE id = 'cninfo'"),
        await sql(
            db, "SELECT consecutive_failures FROM supply_channel WHERE id = 'cninfo'"
        ),
    )
    assert (status, failures) == ("active", 0)


async def test_review_4_stuck_running_jobs_are_recovered(db):
    w = WebWorld(db)
    _, items = await submit(w, "alice", KEY)
    job = await job_of(db, items[0].id)
    [picked] = await w.dispatch()
    assert picked == job
    # 投递丢了：排出去 15 分钟以上还没开始跑
    w.clock.now += timedelta(minutes=16)
    assert await w.dispatch() == [job]
    assert (
        await sql(db, "SELECT last_error FROM supply_job WHERE id = :j", j=job)
        == "dispatch_lost"
    )
    # worker 被杀：一次尝试开始两小时还没结束
    await SqlSupplyRepository(lambda: db).start_attempt(job, w.clock.now)
    w.clock.now += timedelta(hours=1)
    assert await w.dispatch() == []  # 还在时限里
    w.clock.now += timedelta(hours=2)
    assert await w.dispatch() == [job]
    assert (
        await sql(
            db,
            "SELECT outcome FROM supply_attempt WHERE job_id = :j ORDER BY started_at LIMIT 1",
            j=job,
        )
        == "interrupted"
    )


async def test_review_6_changing_an_approved_registration_needs_review_again(db):
    w = WebWorld(db)
    _, items = await submit(w, "alice", KEY)
    job = await job_of(db, items[0].id)
    reg = Registration(
        name="巨潮资讯",
        kind="catalog",
        plugin="cninfo",
        doc_types=("annual_report",),
        readings=("original", "pages", "table"),
        license="sale",
        rate_per_minute=10,
        daily_cap=500,
    )
    changed = await w.desk.catalog.update("cninfo", reg, actor="op", reason="改许可档")
    assert (changed.review, changed.status_reason) == ("pending", "re_review")
    # 已放行渠道改登记待重审：作业等着，新需求仍接进来排作业等重审（三审第 4 条）
    assert await w.runner.run(job) == "held"
    _, other = await submit(w, "bob", year(2024))
    assert other[0].status == "fetching"
    other_job = await job_of(db, other[0].id)
    assert await w.runner.run(other_job) == "held"
    # 要（重新）试采才能放行
    await refused(
        w.desk.catalog.review("cninfo", "approve", actor="op", reason="复核"),
        409,
        "trial_required",
    )
    trial = await w.desk.catalog.request_trial("cninfo", KEY, actor="op")
    await w.desk.trials.run(trial.id)
    await w.desk.catalog.review("cninfo", "approve", actor="op", reason="复核")
    assert (
        await sql(db, "SELECT status FROM supply_job WHERE id = :j", j=job) == "queued"
    )
    assert await w.runner.run(job) == "done"
    assert await w.runner.run(other_job) == "done"


class RefusingReader(FakeReader):
    async def content(self, grant_id, material_id, version_no):
        raise SupplyError(422, "material_withdrawn", "withdrawn")


async def test_review_7_a_failed_upload_processing_writes_a_failed_reading(db):
    w = WebWorld(db)
    w.runner.reader = RefusingReader({})
    w.knowbase.pending = [("alice", "m-1", 1)]
    await ProcessingIntake(w.knowbase, w.schedule, clock=w.clock).pull()
    [job] = await w.dispatch()
    assert await w.runner.run(job) == "failed"
    # 写一条「没成」，knowbase 的待加工清单就不再列它；授权交还
    assert ("failed", "text") in w.knowbase.calls
    assert w.runner.reader.released == ["g-1"]  # type: ignore[union-attr]


class LimitedCatalog(FakeCatalog):
    async def list(self, key):
        raise SupplyError(429, "channel_rate_limited", "slow down")


async def test_review_9_and_10_rate_limits_defer_without_an_attempt(db):
    w = WebWorld(db)
    w.catalogs["cninfo"] = LimitedCatalog()
    _, items = await submit(w, "alice", KEY)
    job = await job_of(db, items[0].id)
    assert await w.runner.run(job) == "deferred"
    status, attempts, not_before = (
        await sql(db, "SELECT status FROM supply_job WHERE id = :j", j=job),
        await sql(db, "SELECT attempts FROM supply_job WHERE id = :j", j=job),
        await sql(db, "SELECT not_before FROM supply_job WHERE id = :j", j=job),
    )
    # 放回队列（同一事务里定了什么时候再排），不算一次尝试
    assert (status, attempts) == ("waiting", 0)
    assert not_before == w.clock.now + timedelta(minutes=10)
    # 临时出错的重试：放回与再排的时间也在同一次结束里定下
    w.catalogs["cninfo"] = FakeCatalog()
    w.knowbase.fail_deliver = SupplyError(503, "knowbase_error", "down")
    assert await w.runner.run(job) == "retry"
    assert await sql(db, "SELECT not_before FROM supply_job WHERE id = :j", j=job) == (
        w.clock.now + timedelta(minutes=1)
    )


async def test_review_13_an_undelivered_original_is_still_purged_after_30_days(db):
    w = WebWorld(db)
    w.knowbase.fail_deliver = SupplyError(422, "subject_invalid", "no")
    _, items = await submit(w, "alice", KEY)
    assert await w.runner.run(await job_of(db, items[0].id)) == "failed"
    assert await sql(db, "SELECT purge_after FROM supply_original") == (
        w.clock.now + timedelta(days=30)
    )


async def test_review_15_concurrent_acceptances_do_not_500(db):
    w = WebWorld(db)
    w.confirm("conf-race", "alice", KEY)

    async def once():
        return await w.demands.submit(
            app="knowbench",
            owner="alice",
            proposer="app",
            chain=(),
            task_ref=None,
            confirmation_id="conf-race",
            idempotency_key="idem-race-0001",
            items=[DemandItemIn(KEY, ("original",), "c1")],
        )

    first, second = await asyncio.gather(once(), once())
    assert first[0].id == second[0].id
    assert sorted([first[2], second[2]]) == [False, True]
    # 不同的人同时要同一份：共享一个作业，不撞唯一索引
    keys = [year(2019)] * 3
    made = await asyncio.gather(
        *(submit(w, f"u-{n}", key) for n, key in enumerate(keys))
    )
    jobs = {await job_of(db, items[0].id) for _, items in made}
    assert len(jobs) == 1


async def test_upload_processing_still_uses_its_own_queue(db):
    """加工队列（不是渠道）的作业照旧能手动再排。"""
    w = WebWorld(db)
    w.knowbase.pending = [("alice", "m-2", 1)]
    await ProcessingIntake(w.knowbase, w.schedule, clock=w.clock).pull()
    job = str(
        await sql(
            db, "SELECT id FROM supply_job WHERE channel_id = :q", q=PROCESS_QUEUE
        )
    )
    detail = await w.desk.monitor.requeue(job, actor="op")
    assert detail["status"] == "queued"


async def test_a_web_trial_fetches_the_sample_page(db):
    w = WebWorld(db)
    w.http.pages = {PAGE: html("<p>样例</p>", PAGE)}
    site = await w.desk.catalog.ensure_site(PAGE)
    trial = await w.desk.catalog.request_trial(site.id, actor="op", url=PAGE)
    assert await w.desk.trials.run(trial.id) == "ok"
    _, _, trials = await w.desk.catalog.detail(site.id)
    assert trials[0].fetched["media_type"].startswith("text/html")  # type: ignore[index]
    # 订阅源、站内搜索也登记得上
    await w.desk.catalog.register(
        "stats-feed",
        replace(
            REG,
            kind="feed",
            plugin="feed",
            domain="data.stats.gov.cn",
            doc_types=("statistical_data",),
        ),
        actor="op",
    )
    await refused(
        w.desk.catalog.register(
            "x-search", replace(REG, kind="search", plugin="feed"), actor="op"
        ),
        422,
        "plugin_kind_mismatch",
    )


async def test_review_14_the_directory_refresh_goes_through_the_cninfo_limiter(
    db, monkeypatch
):
    from app.bootstrap import supply as bootstrap
    from app.infrastructure.storage import postgres
    from app.infrastructure.supply.adapters import LimitedHttp

    seen: list[object] = []

    class Directory:
        async def refresh(self, catalog):
            seen.append(catalog.http)
            return 0

    class Postgres:
        session_factory = db

    monkeypatch.setattr(postgres, "get_postgres", lambda: Postgres())
    monkeypatch.setattr(bootstrap, "build_directory", lambda sessions: Directory())
    assert await bootstrap.build_directory_refresh() == 0
    assert isinstance(seen[0], LimitedHttp)
