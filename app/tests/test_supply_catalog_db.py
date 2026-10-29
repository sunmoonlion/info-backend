"""来源目录（SDD 0045 第 3.1 节、第七节块 2）：登记、试采、审核、在用 / 暂停 / 停用、先后、
连续失败自动暂停、按网站审与待审网站、缺口清单、队列监控。走真数据库，渠道换成假的。"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, timedelta

import pytest
from test_durable_delivery_db import db as db
from test_durable_delivery_db import sql
from test_supply_db import KEY, FakeCatalog, World, refused

from app.application.supply.catalog import ChannelCatalog, TrialRunner
from app.application.supply.monitor import SupplyMonitor
from app.domain.supply.catalog import (
    AUTO_PAUSE_AFTER,
    SITE_REVIEW_WITHIN,
    Registration,
    site_of,
)
from app.domain.supply.models import (
    DemandItem,
    DemandItemIn,
    DescriptionKey,
    SupplyError,
)
from app.domain.supply.quota import Quota
from app.infrastructure.repositories.supply_catalog import (
    TRIAL_TOPIC,
    SqlCatalogRepository,
)
from app.infrastructure.repositories.supply_status import STATUS_TOPIC

REG = Registration(
    name="上交所定期报告",
    kind="catalog",
    plugin="cninfo",
    doc_types=("annual_report",),
    readings=("original", "pages"),
    license="via_expert",
    rate_per_minute=10,
    daily_cap=500,
    domain="sse.com.cn",
    terms_url="https://www.sse.com.cn/terms",
)


class Desk:
    """管理端那一头：来源目录、试采、监控，和需求那一头共用同一个库与时钟。"""

    def __init__(self, db, world: World, catalog=None) -> None:
        self.world = world
        self.repo = SqlCatalogRepository(lambda: db)
        self.catalog = ChannelCatalog(self.repo, clock=world.clock)
        self.catalogs = {"cninfo": catalog or FakeCatalog()}
        self.trials = TrialRunner(
            self.repo,
            lambda channel: self.catalogs.get(channel.plugin),
            clock=world.clock,
        )
        self.monitor = SupplyMonitor(self.repo, clock=world.clock)

    def tick(self, **kw) -> None:
        self.world.clock.now += timedelta(**kw)


async def topics(db, topic: str) -> int:
    return await sql(
        db, "select count(*) from outbox_message where topic = :t", t=topic
    )


async def test_register_trial_and_review_with_a_record_of_who_and_why(db):
    desk = Desk(db, World(db))
    await refused(
        desk.catalog.register("sse", replace(REG, plugin="x"), actor="op"),
        422,
        "plugin_unknown",
    )
    await refused(
        desk.catalog.register("sse", replace(REG, doc_types=("nope",)), actor="op"),
        422,
        "doc_type_unknown",
    )
    made = await desk.catalog.register("sse", REG, actor="op-1")
    assert (made.review, made.status, made.domain) == (
        "pending",
        "active",
        "sse.com.cn",
    )
    # 排在已登记的后面（巨潮、待审的巨潮互动易）
    assert [c.id for c in await desk.catalog.channels()] == [
        "cninfo",
        "cninfo-irm",
        "sse",
    ]
    await refused(desk.catalog.register("sse2", REG, actor="op"), 409, "channel_exists")

    # 放行之前要试采成功
    await refused(
        desk.catalog.review("sse", "approve", actor="op", reason="官方披露"),
        409,
        "trial_required",
    )
    desk.tick(seconds=1)
    trial = await desk.catalog.request_trial("sse", KEY, actor="op-1")
    assert trial.status == "queued" and await topics(db, TRIAL_TOPIC) == 1
    assert await desk.trials.run(trial.id) == "ok"
    assert await desk.trials.run(trial.id) == "skipped"
    _, _, trials = await desk.catalog.detail("sse")
    assert trials[0].listed[0]["title"] == "五粮液2025年年度报告"
    assert trials[0].fetched["size_bytes"] > 0 and trials[0].fetched["sha256"]

    # 试采之后又改了登记：要重新试采
    desk.tick(seconds=1)
    await desk.catalog.update(
        "sse", replace(REG, daily_cap=800), actor="op-2", reason="放宽"
    )
    await refused(
        desk.catalog.review("sse", "approve", actor="op", reason="官方披露"),
        409,
        "trial_outdated",
    )
    desk.tick(seconds=1)
    again = await desk.catalog.request_trial("sse", KEY, actor="op-2")
    await desk.trials.run(again.id)
    await refused(
        desk.catalog.review("sse", "approve", actor="op", reason=" "),
        422,
        "reason_required",
    )
    approved = await desk.catalog.review(
        "sse", "approve", actor="op-3", reason="交易所官方披露", license="sale"
    )
    assert (approved.review, approved.license) == ("approved", "sale")

    _, events, _ = await desk.catalog.detail("sse")
    assert [(e.action, e.actor) for e in events][:2] == [
        ("approve", "op-3"),
        ("update", "op-2"),
    ]
    assert events[0].reason == "交易所官方披露"
    assert events[1].detail["daily_cap"] == {"from": 500, "to": 800}


async def test_a_trial_that_lists_nothing_or_has_no_plugin_fails(db):
    desk = Desk(db, World(db), catalog=FakeCatalog(entries=[]))
    trial = await desk.catalog.request_trial("cninfo", KEY, actor="op")
    assert await desk.trials.run(trial.id) == "failed"
    _, _, trials = await desk.catalog.detail("cninfo")
    assert (trials[0].status, trials[0].error) == ("failed", "nothing_listed")

    site = await desk.catalog.ensure_site("https://www.stats.gov.cn/sj/zxfb/1.html")
    trial = await desk.catalog.request_trial(site.id, KEY, actor="op")
    assert await desk.trials.run(trial.id) == "failed"
    _, _, trials = await desk.catalog.detail(site.id)
    assert trials[0].error == "plugin_unavailable"


async def test_order_decides_which_channel_is_tried_first(db):
    w = World(db)
    desk = Desk(db, w)
    await desk.catalog.register("sse", REG, actor="op")
    t = await desk.catalog.request_trial("sse", KEY, actor="op")
    await desk.trials.run(t.id)
    await desk.catalog.review("sse", "approve", actor="op", reason="官方")
    await refused(
        desk.catalog.reorder(["sse", "nope"], actor="op"), 422, "order_invalid"
    )
    ordered = await desk.catalog.reorder(["sse"], actor="op")
    assert [c.id for c in ordered] == ["sse", "cninfo", "cninfo-irm"]
    w.confirm("conf-1", "alice", KEY)
    _, items, _ = await w.submit()
    assert items[0].channel_id == "sse"


async def test_pause_holds_jobs_resume_requeues_and_disable_withdraws(db):
    w = World(db)
    desk = Desk(db, w)
    w.confirm("conf-1", "alice", KEY)
    demand, _, _ = await w.submit()
    job_id = str(await sql(db, "SELECT id FROM supply_job"))

    await refused(
        desk.catalog.set_status("cninfo", "pause", actor="op", reason=""),
        422,
        "reason_required",
    )
    paused = await desk.catalog.set_status(
        "cninfo", "pause", actor="op", reason="源站改版"
    )
    assert (paused.status, paused.status_reason) == ("paused", "源站改版")
    # 暂停的渠道照样接新需求：作业等着，不变成缺口
    w.confirm("conf-2", "bob", KEY)
    _, items, _ = await w.submit(
        owner="bob", confirmation="conf-2", idem="idem-bob-0001"
    )
    assert items[0].status == "fetching"
    assert await w.runner.run(job_id) == "held"
    assert await sql(db, "SELECT status FROM supply_job") == "waiting"
    assert await sql(db, "SELECT attempts FROM supply_job") == 0

    # 暂停时调度不排它
    assert await w.dispatch() == []
    await desk.catalog.set_status("cninfo", "resume", actor="op", reason=None)
    assert await sql(db, "SELECT status FROM supply_job") == "queued"
    # 恢复后调度下一轮就排出去，经发件箱投给 worker
    before = await sql(
        db, "select count(*) from outbox_message where topic = 'knowfeed.supply.job.v1'"
    )
    assert await w.dispatch() == [job_id]
    after = await sql(
        db, "select count(*) from outbox_message where topic = 'knowfeed.supply.job.v1'"
    )
    assert after == before + 1
    assert await sql(db, "SELECT status FROM supply_job") == "running"

    await refused(
        desk.catalog.set_status("cninfo", "resume", actor="op", reason=None),
        409,
        "status_move_invalid",
    )
    statuses = await topics(db, STATUS_TOPIC)
    await desk.catalog.set_status("cninfo", "disable", actor="op", reason="条款变了")
    _, items = await w.demands.get("knowbench", demand.id)
    assert (items[0].status, items[0].reason) == ("unsatisfiable", "channel_disabled")
    assert await sql(db, "SELECT status FROM supply_job") == "failed"
    # 两张单各通知一次
    assert await topics(db, STATUS_TOPIC) == statuses + 2
    # 停用的渠道不再接：新需求是缺口
    w.confirm("conf-3", "carol", KEY)
    _, items, _ = await w.submit(
        owner="carol", confirmation="conf-3", idem="idem-car-0001"
    )
    assert (items[0].status, items[0].reason) == ("unsatisfiable", "no_channel")
    gaps = await desk.monitor.gaps("uncovered")
    assert [(g["doc_type"], g["items"], g["users"]) for g in gaps] == [
        ("annual_report", 3, 3)
    ]


class BrokenCatalog(FakeCatalog):
    async def list(self, key):
        raise SupplyError(503, "channel_unavailable", "down")


async def test_consecutive_failures_pause_the_channel_and_success_resets(db):
    w = World(db, catalog=BrokenCatalog())
    desk = Desk(db, w)
    for n in range(AUTO_PAUSE_AFTER):
        owner = f"user-{n}"
        key = DescriptionKey(
            "000858.SZ", "annual_report", date(2020 - n, 1, 1), date(2020 - n, 12, 31)
        )
        w.confirm(f"conf-{n}", owner, key)
        await w.submit(
            owner=owner, confirmation=f"conf-{n}", key=key, idem=f"idem-{n}-abcdef"
        )
    jobs = [
        str(i)
        for i in (await _ids(db, "SELECT id FROM supply_job ORDER BY created_at"))
    ]
    for job_id in jobs[:-1]:
        assert await w.runner.run(job_id) == "retry"
    channel = await desk.catalog.get("cninfo")
    assert (channel.status, channel.consecutive_failures) == (
        "active",
        AUTO_PAUSE_AFTER - 1,
    )
    assert await w.runner.run(jobs[-1]) == "retry"
    channel = await desk.catalog.get("cninfo")
    assert channel.status == "paused"
    assert channel.status_reason.startswith("auto_paused")
    _, events, _ = await desk.catalog.detail("cninfo")
    assert (events[0].action, events[0].actor) == ("auto_pause", "system")
    # 暂停后作业不再跑
    assert await w.runner.run(jobs[0]) == "held"

    # 恢复后答了就清零
    await desk.catalog.set_status("cninfo", "resume", actor="op", reason=None)
    w.catalogs["cninfo"] = FakeCatalog()
    assert await w.runner.run(jobs[0]) == "done"
    assert (await desk.catalog.get("cninfo")).consecutive_failures == 0


async def _ids(db, query):
    from sqlalchemy import text

    async with db() as s:
        return [r[0] for r in (await s.execute(text(query))).all()]


async def test_sites_are_reviewed_per_domain_and_expire_without_review(db):
    w = World(db)
    desk = Desk(db, w)
    assert site_of("HTTPS://WWW.Stats.gov.cn:443/a?b=1") == "www.stats.gov.cn"
    with pytest.raises(SupplyError):
        site_of("ftp://example.com/x")
    site = await desk.catalog.ensure_site("https://www.stats.gov.cn/sj/zxfb/1.html")
    assert (site.id, site.kind, site.review) == (
        "site-www-stats-gov-cn",
        "web",
        "pending",
    )
    same = await desk.catalog.ensure_site("http://www.stats.gov.cn/other")
    assert same.id == site.id
    # 已登记的网站（含子域名）照那条走
    assert (
        await desk.catalog.ensure_site("https://static.cninfo.com.cn/f.PDF")
    ).id == "cninfo"

    # 给网址的需求项（随块 4 接上）在等这个网站：这里直接记一项
    w.confirm("conf-1", "alice", KEY)
    demand, _, _ = await w.submit()
    url_demand = replace(
        demand,
        id="6f1c1e1e-0000-4000-8000-000000000001",
        idempotency_key="idem-url-0001",
    )
    await w.repo.submit(
        url_demand,
        [
            DemandItem(
                id="6f1c1e1e-0000-4000-8000-0000000000aa",
                demand_id=url_demand.id,
                key=DescriptionKey("000858.SZ", "webpage"),
                readings=("original",),
                confirmation_item="c1",
                status="accepted",
                created_at=w.clock.now,
                updated_at=w.clock.now,
                channel_id=site.id,
            )
        ],
        [],
        {},
        quota=Quota(),
    )
    summary, rows = await desk.catalog.sites()
    assert summary.pending == 1 and summary.alerts == []
    assert [(r["domain"], r["waiting_items"], r["users"]) for r in rows] == [
        ("www.stats.gov.cn", 1, 1)
    ]
    assert await desk.catalog.expire_sites() == 0
    desk.tick(seconds=int(SITE_REVIEW_WITHIN.total_seconds()) + 60)
    summary, _ = await desk.catalog.sites()
    assert summary.alerts == ["waiting_too_long"]
    assert await desk.catalog.expire_sites() == 1
    reason = await sql(
        db,
        "SELECT reason FROM supply_demand_item WHERE channel_id = :c",
        c=site.id,
    )
    assert reason == "site_review_expired"
    gaps = await desk.monitor.gaps("site")
    assert [(g["kind"], g["doc_type"], g["items"]) for g in gaps] == [
        ("site", "webpage", 1)
    ]
    # 审不过：记原因
    rejected = await desk.catalog.review(site.id, "reject", actor="op", reason="要登录")
    assert rejected.review == "rejected"
    assert (await desk.catalog.sites())[0].pending == 0


async def test_queue_shows_jobs_attempts_and_requeues_waiting_ones(db):
    w = World(db)
    desk = Desk(db, w)
    w.confirm("conf-1", "alice", KEY)
    await w.submit()
    job_id = str(await sql(db, "SELECT id FROM supply_job"))
    queue = await desk.monitor.queue()
    cninfo = next(c for c in queue["channels"] if c["channel_id"] == "cninfo")
    assert (cninfo["queued"], cninfo["running"]) == (1, 0)
    assert queue["tiers"] == [{"tier": 1, "open": 1}]

    w.knowbase.fail_deliver = SupplyError(503, "knowbase_error", "down")
    assert await w.runner.run(job_id) == "retry"
    queue = await desk.monitor.queue()
    assert queue["failures"] == [
        {"channel_id": "cninfo", "reason": "knowbase_error", "count": 1}
    ]
    jobs = await desk.monitor.jobs(channel_id="cninfo", status="waiting", limit=10)
    assert [(j["job_id"], j["waiting_items"]) for j in jobs] == [(job_id, 1)]
    detail = await desk.monitor.job(job_id)
    assert [a["outcome"] for a in detail["attempt_list"]] == ["retry"]
    requeued = await desk.monitor.requeue(job_id)
    assert requeued["status"] == "queued"
    w.knowbase.fail_deliver = None
    assert await w.runner.run(job_id) == "done"
    await refused(desk.monitor.requeue(job_id), 409, "job_not_open")
    await refused(desk.monitor.job("nope"), 404, "job_not_found")


async def test_quota_counts_only_new_acceptances_per_owner(db):
    w = World(db)
    w.demands.quota = Quota(per_hour=3, open_demands=2, monthly_items=3)
    keys = [
        DescriptionKey("000858.SZ", "annual_report", date(y, 1, 1), date(y, 12, 31))
        for y in (2021, 2022, 2023, 2024)
    ]
    w.confirm("conf-q", "alice", *keys)

    async def submit(idem, *picked):
        return await w.demands.submit(
            app="knowbench",
            owner="alice",
            proposer="app",
            chain=(),
            task_ref="task-1",
            confirmation_id="conf-q",
            idempotency_key=idem,
            items=[DemandItemIn(keys[n], ("original",), f"c{n + 1}") for n in picked],
        )

    await submit("idem-q-0001", 0, 1)
    # 重放不扣
    _, _, replayed = await submit("idem-q-0001", 0, 1)
    assert replayed
    # 这个月已新取两份，再要两份超了
    await refused(submit("idem-q-0002", 2, 3), 429, "quota_monthly")
    await submit("idem-q-0003", 2)
    # 两张单都没办完：在途单数满了
    await refused(submit("idem-q-0004", 3), 429, "quota_open_demands")
    # 别人不受影响
    w.confirm("conf-b", "bob", keys[0])
    await w.submit(
        owner="bob", confirmation="conf-b", key=keys[0], idem="idem-bob-0001"
    )
    assert await sql(db, "SELECT count(*) FROM supply_demand") == 3


async def test_quota_limits_how_many_demands_an_hour(db):
    w = World(db)
    w.demands.quota = Quota(per_hour=1)
    w.confirm("conf-1", "alice", KEY)
    demand, _, _ = await w.submit()
    await w.demands.cancel("knowbench", demand.id)
    await refused(w.submit(idem="idem-0002-abcd"), 429, "quota_rate")
    w.clock.now += timedelta(hours=1, seconds=1)
    await w.submit(idem="idem-0003-abcd")
