"""第三轮独立审查 20 条问题的复现与回归测试（真 PostgreSQL 数据库 + 并发与边界复现）。"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from datetime import date, timedelta

import httpx
import pytest
from test_durable_delivery_db import db as db
from test_durable_delivery_db import sql
from test_supply_catalog_db import REG
from test_supply_db import KEY, PDF, Entry, refused
from test_supply_schedule_db import job_of
from test_supply_subscriptions_db import Subs
from test_supply_web_db import PAGE, WebWorld, html

from app.application.ports.supply import Page
from app.application.supply.mine import MyDemands
from app.domain.supply.catalog import Registration
from app.domain.supply.models import DemandItemIn, DescriptionKey, SupplyError
from app.infrastructure.external.knowbase_supply import HttpKnowbase
from app.infrastructure.repositories.supply import SqlSupplyRepository


class StaticToken:
    async def get_token(self) -> str:
        return "svc-token"


# —— 第 1、3、12 条：原件表与交付记录表拆开（不跨区串用）、合规依据 basis、default_description ——


async def test_item_1_3_12_original_delivery_is_isolated_by_zone_and_carries_basis_and_default_desc(
    db,
):
    w = WebWorld(db)
    notice_key = DescriptionKey(
        "000858.SZ",
        "interim_announcement",
        date(2026, 10, 1),
        date(2026, 10, 27),
    )
    notice = Entry(
        "cninfo",
        "000858:notice:same",
        "五粮液临时公告",
        "https://static.cninfo.com.cn/finalpage/2026-10-20/same.PDF",
        date(2026, 10, 20),
        announcement_id="same",
        doc_type="interim_announcement",
        subject="000858.SZ",
    )
    w.catalogs["cninfo"].entries = [notice]

    # 先将 cninfo 设为 collected 区交付一次：产生 alice 的私有资料 m-1-1
    await sql(db, "UPDATE supply_channel SET zone = 'collected' WHERE id = 'cninfo'")
    w.confirm("conf-a", "alice", notice_key)
    d_a, items_a, _ = await w.demands.submit(
        app="knowbench",
        owner="alice",
        proposer="app",
        chain=(),
        task_ref=None,
        confirmation_id="conf-a",
        idempotency_key="idem-zone-a-01",
        items=[DemandItemIn(notice_key, ("original", "pages"), "c1")],
    )
    assert await w.runner.run(await job_of(db, items_a[0].id)) == "done"
    collected_call = w.knowbase.delivered[-1]
    # 第 12 条：default_description 必带 confirmed=False 与条目元数据
    assert collected_call["default_description"] == {
        "title": "五粮液临时公告",
        "doc_type": "interim_announcement",
        "subject": "000858.SZ",
        "disclosed_on": "2026-10-20",
        "edition": "full",
        "topics": [],
        "confirmed": False,
    }
    # 第 3 条：beneficiaries 带该受益人自己的合规依据 basis
    assert collected_call["beneficiaries"][0]["basis"] == {
        "confirmation_id": "conf-a",
        "item_id": "c1",
    }

    # 再把 cninfo 切回 disclosure 区：bob 提同一份公告，绝不能复用 alice 的私有 material_id
    await sql(db, "UPDATE supply_channel SET zone = 'disclosure' WHERE id = 'cninfo'")
    w.confirm("conf-b", "bob", notice_key)
    d_b, items_b, _ = await w.demands.submit(
        app="knowbench",
        owner="bob",
        proposer="app",
        chain=(),
        task_ref=None,
        confirmation_id="conf-b",
        idempotency_key="idem-zone-b-01",
        items=[DemandItemIn(notice_key, ("original", "pages"), "c1")],
    )
    assert await w.runner.run(await job_of(db, items_b[0].id)) == "done"
    _, got_a = await w.demands.get("knowbench", d_a.id)
    _, got_b = await w.demands.get("knowbench", d_b.id)
    assert got_a[0].receipt["material_id"] == "m-1-1"
    assert got_b[0].receipt["material_id"] != got_a[0].receipt["material_id"]
    # 同一字节内容在 supply_original 只存一行，但 supply_original_delivery 分记两行
    assert await sql(db, "SELECT count(*) FROM supply_original") == 1
    assert await sql(db, "SELECT count(*) FROM supply_original_delivery") == 2
    refs = [c[1] for c in w.knowbase.calls if c[0] == "references"][-1]
    assert refs[0].basis == {"confirmation_id": "conf-b", "item_id": "c1"}


async def test_item_3_http_knowbase_sends_basis_in_references_and_subscription_basis():
    from app.domain.supply.models import Demand, DemandItem, Reference, basis_of

    captured: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        import json

        captured.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"recorded": 1})

    kb = HttpKnowbase(
        base_url="https://knowbase.internal/api/internal/v2/user-data",
        tokens=StaticToken(),  # type: ignore[arg-type]
        timeout_seconds=5.0,
        transport=httpx.MockTransport(handler),
    )
    now = KEY.period_start
    assert now is not None
    from datetime import UTC, datetime

    ts = datetime(2026, 10, 27, tzinfo=UTC)
    sub_demand = Demand(
        id="d-sub",
        owner="alice",
        proposer="subscription",
        chain=(),
        app="knowfeed",
        task_ref=None,
        confirmation_id="subscription:sub-123@2",
        idempotency_key="idem-sub-1",
        digest="x",
        created_at=ts,
    )
    sub_item = DemandItem(
        id="i-sub",
        demand_id="d-sub",
        key=KEY,
        readings=("original",),
        confirmation_item="s1",
        status="fetching",
        created_at=ts,
        updated_at=ts,
    )
    basis = basis_of(sub_demand, sub_item)
    assert basis == {"subscription_id": "sub-123", "revision": 2}
    await kb.add_references("mat-1", 1, [Reference("alice", sub_item.basis_ref, basis)])
    assert captured[0]["references"] == [
        {
            "subject": "alice",
            "ref": "demand:d-sub:s1",
            "basis": {"subscription_id": "sub-123", "revision": 2},
        }
    ]


# —— 第 2、11 条：collected 授权回退下一位受益人、已有读法不重做、漏回受益人标失败 ——


async def test_item_2_and_11_collected_grant_fallback_and_missing_beneficiary_fails(db):
    w = WebWorld(db)
    w.http.pages = {PAGE: html("<html><title>公报</title><p>内容正文</p></html>", PAGE)}
    await w.approve_site(PAGE, license="self")
    d_alice, items_a, _ = await w.submit_url("alice", PAGE)
    d_bob, items_b, _ = await w.submit_url("bob", PAGE)
    d_carol, items_c, _ = await w.submit_url("carol", PAGE)
    job = await job_of(db, items_a[0].id)
    assert (
        await job_of(db, items_b[0].id) == job
        and await job_of(db, items_c[0].id) == job
    )

    # 模拟：knowbase 回包漏了 carol（第 11 条），且第一位受益人 alice 领授权报 422（第 2 条）
    orig_deliver = w.knowbase.deliver_collected
    orig_grant = w.knowbase.processing_grant

    async def partial_collected(**kw):
        ans = await orig_deliver(**kw)
        return [a for a in ans if a["subject"] != "carol"]

    async def grant_refuse_alice(material_id, version_no, *, owner="public:disclosure"):
        if owner == "alice":
            raise SupplyError(422, "material_withdrawn", "alice withdrew")
        return await orig_grant(material_id, version_no, owner=owner)

    w.knowbase.deliver_collected = partial_collected  # type: ignore[method-assign]
    w.knowbase.processing_grant = grant_refuse_alice  # type: ignore[method-assign]

    assert await w.runner.run(job) == "done"
    # 换用第二位受益人 bob 的授权完成了加工
    assert ("grant", "bob") in w.knowbase.calls
    _, got_a = await w.demands.get("knowbench", d_alice.id)
    _, got_b = await w.demands.get("knowbench", d_bob.id)
    _, got_c = await w.demands.get("knowbench", d_carol.id)
    assert got_a[0].status == "ready" and got_b[0].status == "ready"
    # carol 因回包缺失标为 failed:unexpected_response，绝不标成空回执 ready
    assert (got_c[0].status, got_c[0].reason, got_c[0].receipt) == (
        "failed",
        "unexpected_response",
        {},
    )


# —— 第 4、14、18 条：登记更新只对实质字段打回重审、非目录型 disclosure 必检 subject、待审网站统计口径 ——


async def test_item_4_14_18_catalog_update_fields_and_disclosure_subject_and_site_counts(
    db,
):
    w = WebWorld(db)
    # 1) 没有任何实质变化的 update 不写空事件、不打回 pending
    before_events = len((await w.desk.catalog.detail("cninfo"))[1])
    cninfo = await w.desk.catalog.get("cninfo")
    same_reg = Registration(
        name=cninfo.name,
        kind=cninfo.kind,
        plugin=cninfo.plugin,
        doc_types=cninfo.doc_types,
        readings=cninfo.readings,
        license=cninfo.license,
        rate_per_minute=cninfo.rate_per_minute,
        daily_cap=cninfo.daily_cap,
        max_running=cninfo.max_running,
        domain=cninfo.domain,
        terms_url=cninfo.terms_url,
        render_allowed=cninfo.render_allowed,
        obey_robots=cninfo.obey_robots,
        config=cninfo.config,
    )
    unchanged = await w.desk.catalog.update(
        "cninfo", same_reg, actor="op", reason="无改动"
    )
    assert unchanged.review == "approved"
    assert len((await w.desk.catalog.detail("cninfo"))[1]) == before_events

    # 2) 仅改运维参数（daily_cap / rate_per_minute / name）不打回 pending
    op_changed = await w.desk.catalog.update(
        "cninfo",
        replace(same_reg, daily_cap=999, rate_per_minute=20),
        actor="op",
        reason="调高配额",
    )
    assert op_changed.review == "approved" and op_changed.daily_cap == 999

    # 3) 第 14 条：非目录型渠道审为 disclosure 区时必须配置合法 config.subject
    site = await w.desk.catalog.ensure_site(PAGE)
    w.http.pages = {PAGE: html("<p>公报</p>", PAGE)}
    trial = await w.desk.catalog.request_trial(site.id, actor="op", url=PAGE)
    assert await w.desk.trials.run(trial.id) == "ok"
    await refused(
        w.desk.catalog.review(
            site.id, "approve", actor="op", reason="官网", zone="disclosure"
        ),
        422,
        "subject_required",
    )

    # 4) 第 18 条：pending_sites 只统计 accepted + site_pending，已完成/已取消项不计入
    d1, items1, _ = await w.submit_url("alice", PAGE)
    d2, items2, _ = await w.submit_url("bob", PAGE)
    assert items1[0].reason == "site_pending" and items2[0].reason == "site_pending"
    await w.demands.cancel("knowbench", d2.id)
    summary, rows = await w.desk.catalog.sites()
    assert summary.pending == 1
    assert rows[0]["waiting_items"] == 1 and rows[0]["users"] == 1


# —— 第 5 条：搜索渠道暂停、停用或跑时不可用，search 作业与 supply_search 及时收口 ——


async def test_item_5_search_channel_pause_or_disable_settles_searching_rounds(db):
    s = Subs(db)
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
    url = "https://www.stats.gov.cn/search?q=%E5%B7%A5%E4%B8%9A"
    s.w.http.pages[url] = html('<a href="/r/1.html">工业一</a>', url)
    t = await s.w.desk.catalog.request_trial("stats-search", actor="op", topic="工业")
    assert await s.w.desk.trials.run(t.id) == "ok"
    await s.w.desk.catalog.review("stats-search", "approve", actor="op", reason="官方")

    # 发起一次搜索后暂停该搜索渠道：search 立即收口，不再占 open_searches 名额
    search = await s.searches.start("alice", "工业", (), 5)
    assert search.status == "searching"
    await s.w.desk.catalog.set_status(
        "stats-search", "pause", actor="op", reason="维护"
    )
    after = await s.searches.get("alice", search.id)
    assert after.status == "ended"
    assert await s.repo.open_searches("alice") == 0


# —— 第 7 条：作业派发代次（dispatch_token）并发护栏 ——


async def test_item_7_stale_worker_token_cannot_hold_start_or_finish_re_dispatched_job(
    db,
):
    w = WebWorld(db)
    w.confirm("conf-1", "alice", KEY)
    _, items, _ = await w.submit()
    job_id = await job_of(db, items[0].id)
    [picked] = await w.dispatch()
    assert picked == job_id
    token_1 = str(
        await sql(db, "SELECT dispatch_token FROM supply_job WHERE id = :j", j=job_id)
    )
    attempt_1 = await w.repo.start_attempt(job_id, w.clock.now, token=token_1)
    assert attempt_1 is not None

    # 2 小时后超时回收并重新派发给 worker 2（产生 token_2）
    w.clock.now += timedelta(hours=2, minutes=1)
    [picked_2] = await w.dispatch()
    assert picked_2 == job_id
    token_2 = str(
        await sql(db, "SELECT dispatch_token FROM supply_job WHERE id = :j", j=job_id)
    )
    assert token_2 != token_1

    # 迟到的 worker 1 拿旧 token_1 调 hold_job / finish_attempt 均被拒，不冲掉 worker 2 的状态
    assert await w.repo.hold_job(job_id, token=token_1) is False
    await w.repo.finish_attempt(
        job_id,
        attempt_1,
        w.clock.now,
        outcome="done",
        reason=None,
        job_status="done",
        token=token_1,
    )
    assert (
        await sql(db, "SELECT status FROM supply_job WHERE id = :j", j=job_id)
        == "running"
    )
    # worker 2 拿 token_2 正常跑完
    assert await w.runner.run(job_id, token=token_2) == "done"
    assert (
        await sql(db, "SELECT status FROM supply_job WHERE id = :j", j=job_id) == "done"
    )


# —— 第 8 条：web 端提交/勾选/建订阅的确认幂等与并发防重 ——


async def test_item_8_mine_submit_resumes_saved_confirmation_without_second_confirm(db):
    s = Subs(db)
    mine = MyDemands(s.w.demands, s.w.repo, s.user, clock=s.w.clock)
    deadline = s.w.clock.now + timedelta(days=2)

    # 第一次提交：让 demands.submit 在 user.confirm 成功后抛异常中断
    orig_submit = s.w.demands.submit

    async def boom(**kw):
        raise RuntimeError("db interrupted after confirm")

    s.w.demands.submit = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        await mine.submit(
            "user-token",
            "alice",
            kind="description",
            items=[DemandItemIn(KEY, ("original",), "c1")],
            deadline=deadline,
            note=None,
            idempotency_key="idem-mine-crash-1",
        )
    assert len([c for c in s.user.calls if c[0] == "confirm"]) == 1

    # 恢复 demands.submit 后用同一幂等键重试：直接复用已记的 confirmation，不再调第二次 confirm
    s.w.demands.submit = orig_submit  # type: ignore[method-assign]
    demand, items, replayed, _ = await mine.submit(
        "user-token",
        "alice",
        kind="description",
        items=[DemandItemIn(KEY, ("original",), "c1")],
        deadline=deadline,
        note=None,
        idempotency_key="idem-mine-crash-1",
    )
    assert demand is not None and len(items) == 1 and not replayed
    assert len([c for c in s.user.calls if c[0] == "confirm"]) == 1


async def test_item_8_concurrent_search_pick_and_subscription_create_only_confirm_once(
    db,
):
    s = Subs(db)
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
    url = "https://www.stats.gov.cn/search?q=%E5%B7%A5%E4%B8%9A"
    s.w.http.pages[url] = html('<a href="/r/1.html">工业一</a>', url)
    t = await s.w.desk.catalog.request_trial("stats-search", actor="op", topic="工业")
    await s.w.desk.trials.run(t.id)
    await s.w.desk.catalog.review("stats-search", "approve", actor="op", reason="官方")

    search = await s.searches.start("alice", "工业", (), 5)
    await s.run_all()
    deadline = s.w.clock.now + timedelta(days=2)

    # 两个并发 pick（不同幂等键）同时勾同一个 search：只允许一个进 user.confirm
    results = await asyncio.gather(
        s.searches.pick(
            "user-token",
            "alice",
            search.id,
            ["k1"],
            readings=("original",),
            idempotency_key="idem-pick-conc-1",
            deadline=deadline,
        ),
        s.searches.pick(
            "user-token",
            "alice",
            search.id,
            ["k1"],
            readings=("original",),
            idempotency_key="idem-pick-conc-2",
            deadline=deadline,
        ),
        return_exceptions=True,
    )
    ok = [r for r in results if not isinstance(r, Exception)]
    errs = [r for r in results if isinstance(r, SupplyError)]
    assert len(ok) == 1 and len(errs) == 1 and errs[0].code == "search_not_ready"
    assert len([c for c in s.user.calls if c[0] == "confirm"]) == 1

    # 订阅配额为 2：3 个并发 create 只允许 2 个调 user.subscribe
    sub_results = await asyncio.gather(
        s.create(subjects=("000858.SZ",), idempotency_key="idem-sub-c1"),
        s.create(subjects=("600519.SH",), idempotency_key="idem-sub-c2"),
        s.create(subjects=("000001.SZ",), idempotency_key="idem-sub-c3"),
        return_exceptions=True,
    )
    sub_ok = [r for r in sub_results if not isinstance(r, Exception)]
    sub_err = [r for r in sub_results if isinstance(r, SupplyError)]
    assert len(sub_ok) == 2 and len(sub_err) == 1
    assert sub_err[0].code == "quota_subscriptions"
    assert len([c for c in s.user.calls if c[0] == "subscribe"]) == 2


# —— 第 9、10 条：并发 tick 不重开轮次；到期优先按 expired 收口而非 consent_withdrawn ——


async def test_item_9_and_10_concurrent_tick_starts_one_round_and_expiry_stays_renewable(
    db,
):
    s = Subs(db)
    sub = await s.create(ends_on=s.today + timedelta(days=5))
    # 两个 beat 并发 tick 同一条到期应查的订阅：只启动一轮
    t1, t2 = await asyncio.gather(s.service.tick(), s.service.tick())
    assert t1["started"] + t2["started"] == 1
    assert (
        await sql(
            db,
            "SELECT count(*) FROM supply_subscription_round WHERE subscription_id = :i",
            i=sub.id,
        )
        == 1
    )
    await s.run_all()
    await s.service.tick()

    # 到了截止日之后且 knowbase 侧同意也因到期不在了：应落 expired（可续订），而非 disabled
    s.w.clock.now += timedelta(days=6)
    s.w.knowbase.subscriptions.pop((sub.id, 1), None)
    await s.service.tick()
    after, _ = await s.service.get("alice", sub.id)
    assert (after.status, after.status_reason) == ("expired", None)


# —— 第 13 条：并发受理待审网站 URL 与审核放行不漏排作业 ——


async def test_item_13_concurrent_url_submit_and_site_approve_never_strands_item(db):
    w = WebWorld(db)
    w.http.pages = {PAGE: html("<p>公报</p>", PAGE)}
    site = await w.desk.catalog.ensure_site(PAGE)
    trial = await w.desk.catalog.request_trial(site.id, actor="op", url=PAGE)
    assert await w.desk.trials.run(trial.id) == "ok"

    # 并发执行：一边 approve 网站，另一边提交该网站上的 URL 需求
    _, (demand, _, _) = await asyncio.gather(
        w.desk.catalog.review(
            site.id, "approve", actor="op", reason="官网", license="self"
        ),
        w.submit_url("alice", PAGE),
    )
    _, items = await w.demands.get("knowbench", demand.id)
    assert items[0].status == "fetching"
    assert items[0].reason is None
    assert await job_of(db, items[0].id) is not None


# —— 第 15、16、19 条：超时未交货可取消、URL 直取 PDF 类型与文件名、内部接口校验 ——


async def test_item_15_16_19_timeout_cancel_direct_pdf_url_and_internal_validation(db):
    w = WebWorld(db)
    # 第 15 条：过了 deadline 转 timeout 且还没拿到回执的项，允许用户取消
    w.confirm("conf-t", "alice", KEY)
    demand, items, _ = await w.demands.submit(
        app="knowbench",
        owner="alice",
        proposer="user",
        chain=(),
        task_ref=None,
        confirmation_id="conf-t",
        idempotency_key="idem-timeout-cancel",
        items=[DemandItemIn(KEY, ("original",), "c1")],
        deadline=w.clock.now + timedelta(hours=1),
    )
    repo = SqlSupplyRepository(lambda: db)
    assert await repo.expire_deadlines(w.clock.now + timedelta(hours=2)) == 1
    assert await w.demands.cancel_mine("alice", demand.id) == 1
    _, after = await w.demands.get("knowbench", demand.id)
    assert (after[0].status, after[0].reason) == ("cancelled", "cancelled")

    # 第 16 条：直接按 URL 取 PDF 文件，默认 doc_type 为 other_text，标题取文件名而非整串 URL
    pdf_url = "https://www.stats.gov.cn/files/report_2025.pdf?token=xyz"
    w.http.pages = {
        PAGE: html("<p>首页</p>", PAGE),
        pdf_url: Page(
            200,
            "application/pdf",
            PDF,
            pdf_url,
            filename="2025统计公报.pdf",
        ),
    }
    await w.approve_site(PAGE, license="self")
    w.confirm_urls("conf-pdf", "alice", pdf_url)
    d_pdf, pdf_items, _ = await w.demands.submit(
        app="knowbench",
        owner="alice",
        proposer="app",
        chain=(),
        task_ref=None,
        confirmation_id="conf-pdf",
        idempotency_key="idem-direct-pdf-1",
        items=[
            DemandItemIn(
                None,
                ("original",),
                "c1",
                url=pdf_url,
                declared_public=True,
            )
        ],
    )
    assert await w.runner.run(await job_of(db, pdf_items[0].id)) == "done"
    _, got_pdf = await w.demands.get("knowbench", d_pdf.id)
    assert got_pdf[0].receipt["doc_type"] == "other_text"
    delivered_pdf = w.knowbase.delivered[-1]
    assert delivered_pdf["default_description"]["title"] == "2025统计公报.pdf"
    assert delivered_pdf["default_description"]["doc_type"] == "other_text"

    # 第 19 条：内部接口提交 URL 项强制要求 declared_public=True，且返回 deadline / note / declared_public
    from app.application.errors.exceptions import AppException
    from app.interfaces.http.internal.supply import (
        DemandIn,
        DemandItemModel,
    )
    from app.interfaces.http.internal.supply import (
        submit as internal_submit,
    )

    w.confirm_urls("conf-int-url", "alice", PAGE)
    with pytest.raises(AppException) as exc_info:
        await internal_submit(
            DemandIn(
                owner="alice",
                proposer="app",
                confirmation_id="conf-int-url",
                idempotency_key="idem-int-url-bad",
                items=[
                    DemandItemModel(
                        confirmation_item="c1",
                        url=PAGE,
                        declared_public=False,
                        readings=["original"],
                    )
                ],
            ),
            app="knowbench",
            service=w.demands,
        )
    assert exc_info.value.code == "public_declaration_required"

    deadline_at = w.clock.now + timedelta(days=1)
    read = await internal_submit(
        DemandIn(
            owner="alice",
            proposer="user",
            confirmation_id="conf-int-url",
            idempotency_key="idem-int-url-ok",
            deadline=deadline_at,
            note="内部代提备注",
            items=[
                DemandItemModel(
                    confirmation_item="c1",
                    url=PAGE,
                    declared_public=True,
                    readings=["original"],
                )
            ],
        ),
        app="knowbench",
        service=w.demands,
    )
    assert read.deadline == deadline_at and read.note == "内部代提备注"
    assert read.items[0].declared_public is True
