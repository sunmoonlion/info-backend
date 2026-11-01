"""第二轮独立审查出的问题的复现（未提交那一批里的 19 条）。走真数据库，网站、knowbase 换成
假的；knowbase 新的「为某用户采集」路与放开的确认接口按 Opus 定的形状用假客户端测。"""

from __future__ import annotations

import asyncio
import re
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
from test_durable_delivery_db import db as db
from test_durable_delivery_db import sql
from test_supply_catalog_db import REG
from test_supply_db import KEY, refused
from test_supply_schedule_db import job_of, submit
from test_supply_subscriptions_db import Subs, _search_channel
from test_supply_web_db import PAGE, SITE, WebWorld, html

from app.application.ports.supply import Page
from app.application.supply.matching import PreMatch
from app.application.supply.mine import MyDemands, confirm_item
from app.domain.supply.models import DemandItemIn, Entry, SupplyError
from app.domain.supply.prematch import PrematchRequestV1
from app.domain.supply.quota import Quota
from app.infrastructure.repositories.supply import SqlSupplyRepository
from app.infrastructure.repositories.supply_schedule import SqlScheduleRepository

ARTICLE = (
    "<html><head><title>公报</title></head><body><article><p>"
    + "国家统计局发布的月度数据，工业增加值同比增长。" * 5
    + "</p></article></body></html>"
)


# —— 一、合规 ——


async def test_1_a_disclosure_site_files_by_channel_config_never_by_user_prefill(db):
    w = WebWorld(db)
    w.http.pages = {PAGE: html(ARTICLE, PAGE)}
    site = await w.desk.catalog.ensure_site(PAGE)
    trial = await w.desk.catalog.request_trial(site.id, actor="op", url=PAGE)
    await w.desk.trials.run(trial.id)
    # 审核定为法定披露区，但渠道配置没写主体：放行时即被拒（三审第 14 条）
    with pytest.raises(SupplyError) as exc:
        await w.desk.catalog.review(
            site.id, "approve", actor="op", reason="政府公报", zone="disclosure"
        )
    assert exc.value.code == "subject_required"
    # 渠道配置写了主体：照渠道的描述交，用户预填的主体、类型一概不用
    await w.desk.catalog.update(
        site.id,
        replace(
            REG,
            name="统计局",
            kind="web",
            plugin="web",
            domain="www.stats.gov.cn",
            doc_types=("statistical_bulletin",),
            config={
                "subject": "org:kb-stats-bureau",
                "doc_type": "statistical_bulletin",
            },
        ),
        actor="op",
        reason="补主体",
    )
    trial = await w.desk.catalog.request_trial(site.id, actor="op", url=PAGE)
    await w.desk.trials.run(trial.id)
    await w.desk.catalog.review(
        site.id, "approve", actor="op", reason="政府公报", zone="disclosure"
    )
    _, items, _ = await w.submit_url("bob", PAGE, n=2)
    assert await w.runner.run(await job_of(db, items[0].id)) == "done"
    sent = w.knowbase.delivered[-1]
    assert (sent["subject"], sent["doc_type"]) == (
        "org:kb-stats-bureau",
        "statistical_bulletin",
    )
    assert "beneficiaries" not in sent  # 法定披露走公开原件层
    assert ("deliver", sent["channel_doc_id"]) in w.knowbase.calls


class NotYetKnowbase:
    """knowbase 的「为某用户采集」路还没上。"""

    def __init__(self, inner) -> None:
        self.inner = inner

    def __getattr__(self, name):
        return getattr(self.inner, name)

    async def deliver_collected(self, **kw):
        raise SupplyError(409, "awaiting_knowbase", "not there")


async def test_1_collected_originals_are_not_delivered_until_knowbase_has_the_route(db):
    w = WebWorld(db)
    w.http.pages = {PAGE: html(ARTICLE, PAGE)}
    await w.approve_site(PAGE)
    w.runner.knowbase = NotYetKnowbase(w.knowbase)  # type: ignore[assignment]
    demand, items, _ = await w.submit_url("alice", PAGE)
    assert await w.runner.run(await job_of(db, items[0].id)) == "failed"
    _, items = await w.demands.get("knowbench", demand.id)
    assert (items[0].status, items[0].reason) == ("unsatisfiable", "awaiting_knowbase")
    # 绝不静默改走公开原件层
    assert not any(c[0] == "deliver" for c in w.knowbase.calls)


async def test_2_collected_pages_need_no_subject(db):
    w = WebWorld(db)
    w.http.pages = {PAGE: html(ARTICLE, PAGE)}
    await w.approve_site(PAGE)
    w.confirm_urls("conf-plain", "carol", PAGE)
    _, items, _ = await w.demands.submit(
        app="knowbench",
        owner="carol",
        proposer="app",
        chain=(),
        task_ref=None,
        confirmation_id="conf-plain",
        idempotency_key="idem-plain-0001",
        items=[DemandItemIn(None, ("original",), "c1", url=PAGE)],
    )
    assert await w.runner.run(await job_of(db, items[0].id)) == "done"
    assert w.knowbase.delivered[-1]["beneficiaries"][0]["prefill"] is None


# —— 3、4：网址本身的问题、落在目录型渠道网站上的网址 ——


class BadUrlHttp:
    async def page(self, url, *, max_bytes, timeout_seconds, fetch, site=None):
        if url.endswith("robots.txt"):
            return Page(404, "text/plain", b"", url)
        raise SupplyError(422, "url_rejected", "crawl_address_forbidden")


async def test_3_url_problems_are_refused_up_front_or_charged_to_the_item(db):
    w = WebWorld(db)
    await refused(
        w.submit_url("alice", "https://www.stats.gov.cn:8443/a.html"),
        422,
        "url_invalid",
    )
    w.http.pages = {PAGE: html(ARTICLE, PAGE)}
    await w.approve_site(PAGE)
    w.http = BadUrlHttp()  # type: ignore[assignment]
    for n in range(6):
        _, items, _ = await w.submit_url(f"u-{n}", f"{PAGE}?n={n}", n=n)
        assert await w.runner.run(await job_of(db, items[0].id)) == "absent"
    _, items = await w.demands.get("knowbench", items[0].demand_id)
    assert (items[0].status, items[0].reason) == ("unsatisfiable", "url_rejected")
    status, failures = (
        await sql(db, "SELECT status FROM supply_channel WHERE id = :i", i=SITE),
        await sql(
            db, "SELECT consecutive_failures FROM supply_channel WHERE id = :i", i=SITE
        ),
    )
    assert (status, failures) == ("active", 0)  # 用户的网址打不暂停审过的网站


async def test_4_urls_on_a_catalog_site_are_asked_for_by_description(db):
    w = WebWorld(db)
    pdf = "http://static.cninfo.com.cn/finalpage/2026-04-25/1.PDF"
    _, items, _ = await w.submit_url("alice", pdf)
    assert (items[0].status, items[0].reason) == ("unsatisfiable", "catalog_site")
    prematch = PreMatch(
        w.repo,
        w.desk.repo,
        SqlScheduleRepository(w.repo._sessions),
        global_rate=60,
        clock=w.clock,
    )
    [answer] = await prematch.check(
        "knowbench",
        PrematchRequestV1(items=[{"ref": "u", "url": pdf}]),  # type: ignore[list-item]
    )
    assert (answer.status, answer.reason) == ("unavailable", "catalog_site")


# —— 二、主流程 ——


async def test_5_data_arriving_after_a_timeout_still_lands_in_the_users_column(db):
    w = WebWorld(db)
    w.confirm("conf-t", "alice", KEY)
    demand, items, _ = await w.demands.submit(
        app="knowbench",
        owner="alice",
        proposer="app",
        chain=(),
        task_ref=None,
        confirmation_id="conf-t",
        idempotency_key="idem-timeout-1",
        items=[DemandItemIn(KEY, ("original",), "c1")],
        deadline=w.clock.now + timedelta(hours=1),
    )
    job = await job_of(db, items[0].id)
    await SqlSupplyRepository(lambda: db).expire_deadlines(
        w.clock.now + timedelta(hours=2)
    )
    pushed = await sql(
        db,
        "select count(*) from outbox_message where topic like 'knowfeed.supply.demand.status%'",
    )
    assert await w.runner.run(job) == "done"
    _, items = await w.demands.get("knowbench", demand.id)
    # 状态不再前进，回执记上、引用记上、照样推一次状态
    assert items[0].status == "timeout" and items[0].receipt["material_id"]
    assert any(c[0] == "references" for c in w.knowbase.calls)
    assert (
        await sql(
            db,
            "select count(*) from outbox_message where topic like 'knowfeed.supply.demand.status%'",
        )
        == pushed + 1
    )


async def test_6_only_the_worker_holding_the_dispatch_token_runs_the_job(db):
    w = WebWorld(db)
    _, items = await submit(w, "alice", KEY)
    job = await job_of(db, items[0].id)
    [picked] = await w.dispatch()
    token = str(
        await sql(db, "SELECT dispatch_token FROM supply_job WHERE id = :j", j=job)
    )
    payload = await sql(
        db,
        "SELECT payload->>'dispatch_token' FROM outbox_message "
        "WHERE topic = 'knowfeed.supply.job.v1' ORDER BY created_at DESC LIMIT 1",
    )
    assert payload == token
    assert await w.runner.run(job, "not-this-one") == "skipped"
    # 另一个 worker 正跑着（有没结束的尝试）：同一代也不放行
    attempt = await SqlSupplyRepository(lambda: db).start_attempt(
        job, w.clock.now, token=token
    )
    assert attempt is not None
    assert await w.runner.run(job, token) == "skipped"
    # 手动重排只对卡住的在跑生效
    await refused(w.desk.monitor.requeue(job, actor="op"), 409, "job_still_running")
    w.clock.now += timedelta(hours=3)
    assert (await w.desk.monitor.requeue(job, actor="op"))["status"] == "queued"
    assert (
        await sql(db, "SELECT dispatch_token FROM supply_job WHERE id = :j", j=job)
        is None
    )


async def test_7_subscription_rounds_respect_the_owners_monthly_quota(db):
    s = Subs(db)
    sub = await s.create(monthly_cap=10)
    s.service.quota = Quota(subscriptions=5, monthly_items=2)
    # 本人这个月已经亲手取了 1 份
    await submit(s.w, "alice", KEY)
    await s.service.tick()
    await s.run_all()
    await s.service.tick()
    s.w.clock.now += timedelta(days=1)
    for n in (1, 2, 3):
        s.catalog.add(n, s.today)
    await s.service.tick()
    await s.run_all()
    await s.service.tick()
    _, rounds = await s.service.get("alice", sub.id)
    # 每人每月 2 份，已用 1 份：这一轮只取 1 份，其余记「本月超额未取」
    assert (rounds[0].taken, rounds[0].over_cap) == (1, 2)
    # 订阅生成的单不挤本人亲手提的在途单数
    s.w.demands.quota = Quota(per_hour=60, open_demands=2, monthly_items=50)
    for n in (2021, 2022):
        from test_supply_schedule_db import year

        await submit(s.w, "alice", year(n))


async def test_8_expired_or_withdrawn_subscriptions_stop_at_once(db):
    s = Subs(db)
    sub = await s.create(ends_on=s.today + timedelta(days=2))
    await s.service.tick()  # 一轮开始了，作业还没跑
    s.w.clock.now += timedelta(days=3)
    # 过了截止日：不看下次查的时间、也不等这一轮跑完，马上转「已到期」
    assert (await s.service.tick())["ended"] == 1
    sub, _ = await s.service.get("alice", sub.id)
    assert (sub.status, sub.round_id) == ("expired", None)
    # 这一轮跑的中途撤了同意：结这一轮之前再问一次，不生成需求单
    other = await s.create(subjects=("600519.SH",))
    await s.service.tick()
    s.w.clock.now += timedelta(days=1)
    s.catalog.add(9, s.today)
    s.catalog.notices[-1] = replace(s.catalog.notices[-1], channel_doc_id="600519:9")
    await s.service.tick()
    await s.run_all()
    del s.w.knowbase.subscriptions[(other.id, 1)]
    await s.service.tick()
    other, rounds = await s.service.get("alice", other.id)
    assert other.status == "disabled"
    assert all(r.demand_id is None for r in rounds)


async def test_9_partial_updates_do_not_clobber_each_other(db):
    s = Subs(db)
    sub = await s.create()
    stale = sub
    await s.service.pause("alice", sub.id)
    # 拿着暂停前的那份去改「下月补不补」，不会把状态改回生效中
    await s.service.set_skip_backlog("alice", stale.id, True)
    now, _ = await s.service.get("alice", sub.id)
    assert (now.status, now.skip_backlog) == ("paused", True)
    # 续订两次撞一起：后到的那次修订对不上，拒
    await s.service.renew("user-token", "alice", sub.id, s.today + timedelta(days=300))
    s.service.repo.get = _stale(s.service.repo.get, stale)  # type: ignore[method-assign]
    await refused(
        s.service.renew("user-token", "alice", sub.id, s.today + timedelta(days=310)),
        409,
        "subscription_changed",
    )


def _stale(get, stale):
    async def fake(subscription_id):
        return stale

    return fake


async def test_10_url_confirmations_cover_follow_links(db):
    w = WebWorld(db)
    w.confirm_urls("conf-f", "alice", PAGE, follow=False)
    await refused(
        w.demands.submit(
            app="knowbench",
            owner="alice",
            proposer="app",
            chain=(),
            task_ref=None,
            confirmation_id="conf-f",
            idempotency_key="idem-follow-1",
            items=[
                DemandItemIn(None, ("original",), "c1", url=PAGE, follow_links=True)
            ],
        ),
        403,
        "outside_confirmation",
    )
    assert confirm_item(
        DemandItemIn(None, ("original",), "c1", url=PAGE, follow_links=True)
    ) == {"url": PAGE, "follow_links": True}


KNOWBASE_SCHEMAS = (
    Path(__file__).resolve().parents[4]
    / "knowbase-app/knowbase-backend/app/app/interfaces/schemas/user_resolution.py"
)


def test_11_confirm_items_match_the_knowbase_confirm_contract():
    """我们发的确认条目与 knowbase `ConfirmIn` 对得上（并列检出 knowbase-app 时核源码）：
    条目的栏位都在 `ConfirmItemIn` 里（`follow_links` 是 knowbase 这次要加的），整体是
    kind、items、supply；回包有 confirmation_id、items[].item_id、branches[]。"""
    sent = [
        confirm_item(DemandItemIn(KEY, ("original", "pages"), "c1")),
        confirm_item(
            DemandItemIn(None, ("original",), "c1", url=PAGE, prefill={"title": "公报"})
        ),
    ]
    if not KNOWBASE_SCHEMAS.exists():
        pytest.skip("knowbase-app is not checked out next to knowfeed-app")
    source = KNOWBASE_SCHEMAS.read_text(encoding="utf-8")

    def fields(name: str) -> set[str]:
        block = source.split(f"class {name}(BaseModel):", 1)[1].split("\nclass ", 1)[0]
        return set(re.findall(r"^    (\w+):", block, re.MULTILINE)) - {"model_config"}

    allowed = fields("ConfirmItemIn") | {"follow_links"}  # knowbase 这次加
    for item in sent:
        assert set(item) <= allowed, set(item) - allowed
    assert {"kind", "items", "supply"} <= fields("ConfirmIn")
    assert {"confirmation_id", "items", "branches"} <= fields("ConfirmationRead")
    assert {"item_id", "branch"} <= fields("BranchRead")


class CountingUser:
    def __init__(self, knowbase) -> None:
        self.knowbase = knowbase
        self.confirms = 0
        self.ready = False

    async def confirm(self, token, *, kind, items, supply):
        self.confirms += 1
        await asyncio.sleep(0.05)
        number = f"conf-web-{self.confirms}"
        made = [{"item_id": f"c{n + 1}", **i} for n, i in enumerate(items)]
        self.knowbase.confirmations[number] = {
            "subject": "alice",
            "items": made,
            "supply": supply,
        }
        branch = "ready" if self.ready else "supply"
        return {
            "confirmation_id": number,
            "items": made,
            "branches": [{"item_id": i["item_id"], "branch": branch} for i in made],
        }


async def test_12_web_submissions_claim_their_key_before_confirming(db):
    w = WebWorld(db)
    user = CountingUser(w.knowbase)
    mine = MyDemands(w.demands, w.repo, user, clock=w.clock)  # type: ignore[arg-type]
    deadline = w.clock.now + timedelta(days=2)

    async def send(key="web-0001-abcd", readings=("original",)):
        return await mine.submit(
            "t",
            "alice",
            kind="description",
            items=[DemandItemIn(KEY, readings, "c1")],
            deadline=deadline,
            note=None,
            idempotency_key=key,
        )

    results = await asyncio.gather(send(), send(), return_exceptions=True)
    assert user.confirms == 1  # 并发的第二次不再记一份孤儿确认
    errors = [r for r in results if isinstance(r, SupplyError)]
    assert [e.code for e in errors] in ([], ["submission_in_progress"])
    replay = await send()
    assert replay[2] is True and user.confirms == 1
    await refused(send(readings=("original", "pages")), 409, "idempotency_key_reused")
    # 全都已有的也落一条记录：重放不再记确认
    user.ready = True
    first = await send(key="web-0002-abcd")
    again = await send(key="web-0002-abcd")
    assert first[0] is None and again[2] is True and again[3] == ["c1"]
    assert user.confirms == 2


async def test_13_topic_searches_have_a_rate_and_reuse_recent_results(db):
    from app.application.supply import searches as module

    s = Subs(db)
    await _search_channel(s)
    first = await s.searches.start("alice", "工业", (), 5)
    await s.run_all()
    # 十分钟内同一话题：直接用上次的结果，不再问渠道
    jobs = await sql(db, "SELECT count(*) FROM supply_job WHERE kind = 'search'")
    again = await s.searches.start("bob", "工业", (), 5)
    assert again.status == "ready" and len(again.candidates) == 2
    assert (
        await sql(db, "SELECT count(*) FROM supply_job WHERE kind = 'search'") == jobs
    )
    assert first.id != again.id
    # 同时在找的数、每小时的次数
    old = module.OPEN_SEARCHES
    module.OPEN_SEARCHES = 1
    try:
        await s.searches.start("carol", "能源", (), 5)
        await refused(
            s.searches.start("carol", "钢铁", (), 5), 429, "quota_open_searches"
        )
    finally:
        module.OPEN_SEARCHES = old
    module.SEARCHES_PER_HOUR, old = 1, module.SEARCHES_PER_HOUR
    try:
        await refused(
            s.searches.start("alice", "煤炭", (), 5), 429, "quota_search_rate"
        )
    finally:
        module.SEARCHES_PER_HOUR = old


async def test_15_cancelling_withdraws_every_revision(db):
    s = Subs(db)
    sub = await s.create()
    await s.service.renew("user-token", "alice", sub.id, s.today + timedelta(days=300))
    await s.service.cancel("user-token", "alice", sub.id)
    assert [c for c in s.user.calls if c[0] == "unsubscribe"] == [
        ("unsubscribe", sub.id, 2),
        ("unsubscribe", sub.id, 1),
    ]


async def test_17_the_public_page_declaration_is_recorded(db):
    w = WebWorld(db)
    w.confirm_urls("conf-p", "alice", PAGE)
    _, items, _ = await w.demands.submit(
        app="knowbench",
        owner="alice",
        proposer="app",
        chain=(),
        task_ref=None,
        confirmation_id="conf-p",
        idempotency_key="idem-public-1",
        items=[DemandItemIn(None, ("original",), "c1", url=PAGE, declared_public=True)],
    )
    assert items[0].declared_public is True
    mine = MyDemands(w.demands, w.repo, CountingUser(w.knowbase), clock=w.clock)  # type: ignore[arg-type]
    await refused(
        mine.submit(
            "t",
            "alice",
            kind="url",
            items=[DemandItemIn(None, ("original",), "c1", url=PAGE)],
            deadline=w.clock.now + timedelta(days=1),
            note=None,
            idempotency_key="web-public-0001",
        ),
        422,
        "public_declaration_required",
    )


async def test_19_fresh_skips_discoveries_before_the_subscription_started(db):
    s = Subs(db)
    sub = await s.create()
    entry = Entry(
        "cninfo",
        "000858:notice:old",
        "旧公告",
        "http://x/1.PDF",
        s.today - timedelta(days=5),
        doc_type="interim_announcement",
    )
    await s.repo.save_watch(
        "cninfo",
        sub.targets()[0],
        [("000858:notice:old", entry.as_dict(), entry.disclosed_on)],
        {},
        s.w.clock.now,
    )
    assert await s.repo.fresh(sub, [("cninfo", sub.targets()[0])]) == []
    assert await s.repo.prune_discoveries(s.w.clock.now + timedelta(days=1)) == 1
