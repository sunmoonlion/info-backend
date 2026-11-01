"""调度、加工、切合（SDD 0045 第 3.3、3.4 节、第四节；发版前补齐 F6、F7、F9）。

按渠道排队、四档、按用户轮流、限速与日上限、放回队列；取不到换下一个渠道、供给条件快照；
预切合；上传资料的加工。走真数据库，渠道、knowbase 换成假的。
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta

import pytest
from test_durable_delivery_db import db as db
from test_durable_delivery_db import sql
from test_supply_catalog_db import REG, Desk
from test_supply_db import KEY, PDF, FakeCatalog, World, refused

from app.application.supply.matching import PreMatch
from app.application.supply.scheduler import ChannelLimiter, ProcessingIntake
from app.domain.supply.models import DemandItemIn, DescriptionKey, Entry, SupplyError
from app.domain.supply.prematch import PrematchRequestV1
from app.domain.supply.schedule import (
    PROCESS_QUEUE,
    Candidate,
    ChannelDeferred,
    next_day,
    pick,
)


def year(n: int) -> DescriptionKey:
    return DescriptionKey("000858.SZ", "annual_report", date(n, 1, 1), date(n, 12, 31))


async def submit(w: World, owner: str, key: DescriptionKey, *, app="knowbench", n=1):
    """以某个应用的名义提一张单（确认先记好）。"""
    confirmation = f"conf-{owner}-{key.period_end}-{n}"
    w.confirm(confirmation, owner, key)
    demand, items, _ = await w.demands.submit(
        app=app,
        owner=owner,
        proposer="app",
        chain=(),
        task_ref="task-1",
        confirmation_id=confirmation,
        idempotency_key=f"idem-{owner}-{key.period_end}-{n}",
        items=[DemandItemIn(key, ("original",), "c1")],
    )
    return demand, items


async def job_of(db, item_id: str) -> str:
    return str(
        await sql(db, "SELECT job_id FROM supply_demand_item WHERE id = :i", i=item_id)
    )


async def approve(desk: Desk, channel_id: str, registration, license=None) -> None:
    await desk.catalog.register(channel_id, registration, actor="op")
    trial = await desk.catalog.request_trial(channel_id, KEY, actor="op")
    await desk.trials.run(trial.id)
    await desk.catalog.review(
        channel_id,
        "approve",
        actor="op",
        reason="审过",
        license=license,
        zone="disclosure",  # 测试里的目录型渠道当法定披露
    )


# —— 四档与按用户轮流 ——


def test_pick_takes_the_front_tier_and_turns_users():
    now = datetime(2026, 10, 30, 12, 0).astimezone()
    t = [now - timedelta(minutes=10 - n) for n in range(6)]
    queued = [
        Candidate("lab", 2, t[0], ("carol",)),
        Candidate("a1", 1, t[1], ("alice",)),
        Candidate("a2", 1, t[2], ("alice",)),
        Candidate("a3", 1, t[3], ("alice",)),
        Candidate("b1", 1, t[4], ("bob",)),
    ]
    # 一个人一次要几份不挤掉别人：alice 一个、bob 一个，再轮回 alice；第 ② 档最后
    assert [j for j, _ in pick(queued, 5, {}, now)] == ["a1", "b1", "a2", "a3", "lab"]
    # bob 刚轮到过：alice 先
    assert pick(queued, 1, {"bob": now, "alice": now - timedelta(hours=1)}, now) == [
        ("a1", "alice")
    ]
    # 共享的作业算等它的人里最久没轮到的那个
    shared = [
        Candidate("s", 1, t[0], ("alice", "dave")),
        Candidate("b", 1, t[1], ("bob",)),
    ]
    assert pick(shared, 1, {"alice": now, "bob": now - timedelta(minutes=1)}, now) == [
        ("s", "dave")
    ]


async def test_dispatch_fills_each_channel_by_tier_and_turn(db):
    w = World(db)
    await sql(db, "UPDATE supply_channel SET max_running = 2 WHERE id = 'cninfo'")
    _, lab = await submit(w, "carol", year(2020), app="knowlab")
    w.clock.now += timedelta(seconds=1)
    alice = []
    for n in (2021, 2022, 2023):
        _, items = await submit(w, "alice", year(n))
        alice.append(items[0])
        w.clock.now += timedelta(seconds=1)
    _, bob = await submit(w, "bob", year(2024))
    tiers = await sql(
        db, "SELECT string_agg(tier::text, ',' ORDER BY created_at) FROM supply_job"
    )
    assert tiers == "2,1,1,1,1"  # knowlab 提的第 ② 档

    first = await w.dispatch()
    assert first == [await job_of(db, alice[0].id), await job_of(db, bob[0].id)]
    # 同时最多跑两个：满了不再排
    assert await w.dispatch() == []
    outbox = await sql(
        db,
        "select count(*) from outbox_message where topic = 'knowfeed.supply.job.v1'",
    )
    assert outbox == 2
    assert await w.runner.run(first[0]) == "done"
    # 空出一个：还是第 ① 档的 alice 先，knowlab 的排在后面
    assert await w.dispatch() == [await job_of(db, alice[1].id)]
    turns = await sql(db, "SELECT count(*) FROM supply_turn WHERE queue = 'cninfo'")
    assert turns == 2
    assert lab[0].status == "fetching"


async def test_shared_job_moves_up_to_the_better_tier(db):
    w = World(db)
    await submit(w, "carol", KEY, app="knowlab")
    assert await sql(db, "SELECT tier FROM supply_job") == 2
    await submit(w, "alice", KEY)
    assert await sql(db, "SELECT count(*) FROM supply_job") == 1
    assert await sql(db, "SELECT tier FROM supply_job") == 1


async def test_a_job_nobody_waits_for_is_cancelled_not_run(db):
    w = World(db)
    demand, _ = await submit(w, "alice", KEY)
    await w.demands.cancel("knowbench", demand.id)
    assert await w.dispatch() == []
    assert await sql(db, "SELECT status FROM supply_job") == "failed"
    assert await sql(db, "SELECT last_error FROM supply_job") == "no_one_waiting"


async def test_a_channel_whose_daily_cap_is_used_up_waits_for_tomorrow(db):
    w = World(db)
    await sql(db, "UPDATE supply_channel SET daily_cap = 1 WHERE id = 'cninfo'")
    await submit(w, "alice", KEY)
    await sql(
        db,
        "INSERT INTO supply_channel_request (channel_id, kind, at) "
        "VALUES ('cninfo', 'fetch', :at)",
        at=w.clock.now,
    )
    assert await w.dispatch() == []
    w.clock.now = next_day(w.clock.now) + timedelta(minutes=1)
    assert len(await w.dispatch()) == 1


# —— 限速 ——


async def test_requests_are_limited_per_channel_globally_and_per_day(db):
    w = World(db)
    now = w.clock.now

    async def take(channel="cninfo", *, fetch=False, rate=2, cap=2, global_rate=3):
        return await w.schedule.take(
            channel,
            fetch=fetch,
            rate_per_minute=rate,
            daily_cap=cap,
            global_rate=global_rate,
            now=w.clock.now,
        )

    assert await take() is None
    w.clock.now += timedelta(seconds=10)
    assert await take(fetch=True) is None
    # 这条渠道一分钟两次用完：最早那次满一分钟以后才能再问
    assert await take() == now + timedelta(minutes=1)
    # 别的渠道还能问，但出站地址全局一分钟三次
    assert await take("other") is None
    assert await take("another") == now + timedelta(minutes=1)
    # 过了一分钟又能问；今天取回的份数照算
    w.clock.now = now + timedelta(minutes=2)
    assert await take(fetch=True) is None
    assert await take(fetch=True) == next_day(w.clock.now)
    usage = await w.schedule.usage("cninfo", w.clock.now)
    assert usage.fetched_today == 2
    # 两天前的记录删掉
    w.clock.now += timedelta(days=3)
    assert await w.scheduler.prune() == 4


async def test_limiter_waits_a_little_inline_and_defers_long_waits(db):
    w = World(db)
    slept: list[float] = []

    async def sleep(seconds: float) -> None:
        slept.append(seconds)
        w.clock.now += timedelta(seconds=seconds)

    limiter = ChannelLimiter(
        w.schedule, global_rate=100, clock=w.clock, sleep=sleep, inline_wait=60
    )
    channel = replace(
        await w.repo.get_channel("cninfo"), rate_per_minute=1, daily_cap=1
    )
    await limiter.acquire(channel, fetch=True)
    await limiter.acquire(channel, fetch=False)  # 等满一分钟再问
    assert slept == [60.0]
    with pytest.raises(ChannelDeferred) as caught:
        await limiter.acquire(channel, fetch=True)  # 今天的一份用完了
    assert caught.value.reason == "channel_daily_cap"
    assert caught.value.until == next_day(w.clock.now)
    short = ChannelLimiter(w.schedule, global_rate=100, clock=w.clock, sleep=sleep)
    with pytest.raises(ChannelDeferred) as caught:
        await short.acquire(channel, fetch=False)
    assert caught.value.reason == "rate_limited"


class DeferredCatalog(FakeCatalog):
    def __init__(self, until) -> None:
        super().__init__()
        self.until = until

    async def list(self, key):
        raise ChannelDeferred(self.until, "channel_daily_cap")


async def test_a_deferred_job_goes_back_to_the_queue_without_using_an_attempt(db):
    w = World(db)
    until = next_day(w.clock.now)
    w.catalogs["cninfo"] = DeferredCatalog(until)
    demand, _ = await submit(w, "alice", KEY)
    [job_id] = await w.dispatch()
    assert await w.runner.run(job_id) == "deferred"
    assert await sql(db, "SELECT status FROM supply_job") == "waiting"
    assert await sql(db, "SELECT attempts FROM supply_job") == 0
    assert await sql(db, "SELECT not_before FROM supply_job") == until
    # 渠道没记失败；需求照旧在取
    assert (await w.repo.get_channel("cninfo")).consecutive_failures == 0
    _, items = await w.demands.get("knowbench", demand.id)
    assert items[0].status == "fetching"
    assert await w.dispatch() == []
    w.clock.now = until + timedelta(seconds=1)
    assert await w.dispatch() == [job_id]


# —— 换下一个渠道、供给条件 ——


class RefusingCatalog(FakeCatalog):
    async def list(self, key):
        raise SupplyError(422, "channel_refused", "the channel answered 403")


class BrokenCatalog(FakeCatalog):
    async def list(self, key):
        raise SupplyError(503, "channel_unavailable", "the channel answered 502")


async def test_an_absent_document_moves_to_the_next_channel(db):
    w = World(db)
    desk = Desk(db, w)
    desk.catalogs["cninfo-irm"] = FakeCatalog()
    # 第二条渠道（插件随便选一个，假的插件按名字换）
    await approve(desk, "sse", replace(REG, plugin="cninfo-irm"))
    w.catalogs["cninfo"] = FakeCatalog(entries=[])
    w.catalogs["cninfo-irm"] = FakeCatalog()
    demand, items = await submit(w, "alice", KEY)
    assert items[0].channel_id == "cninfo"
    first = await job_of(db, items[0].id)
    assert await w.runner.run(first) == "moved"
    _, items = await w.demands.get("knowbench", demand.id)
    assert (items[0].status, items[0].channel_id, items[0].tried_channels) == (
        "fetching",
        "sse",
        ("cninfo",),
    )
    second = await job_of(db, items[0].id)
    assert second != first
    assert await sql(db, "SELECT tier FROM supply_job WHERE id = :i", i=second) == 1
    assert await w.runner.run(second) == "done"
    _, items = await w.demands.get("knowbench", demand.id)
    assert (items[0].status, items[0].reason) == ("ready", None)


async def test_when_every_channel_fails_the_item_ends_with_the_right_reason(db):
    w = World(db)
    desk = Desk(db, w)
    desk.catalogs["cninfo-irm"] = FakeCatalog()
    await approve(desk, "sse", replace(REG, plugin="cninfo-irm"))
    w.catalogs["cninfo"] = FakeCatalog(entries=[])
    w.catalogs["cninfo-irm"] = FakeCatalog(entries=[])
    d1, items = await submit(w, "alice", KEY)
    await w.runner.run(await job_of(db, items[0].id))
    await w.runner.run(await job_of(db, items[0].id))
    _, items = await w.demands.get("knowbench", d1.id)
    assert (items[0].status, items[0].reason) == ("unsatisfiable", "channel_has_none")

    # 一条没有、一条回 4xx：4xx 也是「这一条没有」（独立审第 3 条），结局是「渠道里没有」，
    # 也不计渠道的连续失败
    w.catalogs["cninfo-irm"] = RefusingCatalog()
    d2, items = await submit(w, "bob", year(2024))
    await w.runner.run(await job_of(db, items[0].id))
    assert await w.runner.run(await job_of(db, items[0].id)) == "absent"
    _, items = await w.demands.get("knowbench", d2.id)
    assert (items[0].status, items[0].reason) == ("unsatisfiable", "channel_has_none")
    assert (
        await sql(db, "SELECT consecutive_failures FROM supply_channel WHERE id='sse'")
        == 0
    )

    # 一条没有、一条取不成（对方 5xx，重试用完）：结局是「失败」，原因是取不成的那条
    w.catalogs["cninfo-irm"] = BrokenCatalog()
    d3, items = await submit(w, "carol", year(2023))
    await w.runner.run(await job_of(db, items[0].id))
    job = await job_of(db, items[0].id)
    await sql(db, "UPDATE supply_job SET attempts = 5 WHERE id = :j", j=job)
    assert await w.runner.run(job) == "failed"
    _, items = await w.demands.get("knowbench", d3.id)
    assert (items[0].status, items[0].reason) == ("failed", "channel_unavailable")


async def test_the_confirmed_supply_conditions_choose_the_channel(db):
    w = World(db)
    desk = Desk(db, w)
    await approve(desk, "narrow", replace(REG, license="self"), license="self")
    await desk.catalog.reorder(["narrow"], actor="op")
    # 不限条件：先试排在前面的
    _, items = await submit(w, "alice", KEY)
    assert items[0].channel_id == "narrow"
    # 要随公开专家用：只许本人用的那条不满足，跳过
    wide = {"conditions": {"min_license": "via_expert"}}
    w.confirm("conf-wide", "bob", KEY, supply=wide)
    _, items, _ = await w.demands.submit(
        app="knowbench",
        owner="bob",
        proposer="app",
        chain=(),
        task_ref=None,
        confirmation_id="conf-wide",
        idempotency_key="idem-wide-0001",
        items=[DemandItemIn(KEY, ("original",), "c1")],
    )
    assert items[0].channel_id == "cninfo"
    assert await sql(
        db, "SELECT conditions FROM supply_demand WHERE owner_subject = 'bob'"
    ) == {"min_license": "via_expert"}
    # 只许用那条窄的、又要随公开专家用：当前供给条件不满足
    w.confirm(
        "conf-none",
        "carol",
        KEY,
        supply={"conditions": {"min_license": "via_expert", "channels": ["narrow"]}},
    )
    _, items, _ = await w.demands.submit(
        app="knowbench",
        owner="carol",
        proposer="app",
        chain=(),
        task_ref=None,
        confirmation_id="conf-none",
        idempotency_key="idem-none-0001",
        items=[DemandItemIn(KEY, ("original",), "c1")],
    )
    assert (items[0].status, items[0].reason) == ("unsatisfiable", "conditions_unmet")
    w.confirm("conf-bad", "dave", KEY, supply={"conditions": {"min_license": "all"}})
    await refused(
        w.demands.submit(
            app="knowbench",
            owner="dave",
            proposer="app",
            chain=(),
            task_ref=None,
            confirmation_id="conf-bad",
            idempotency_key="idem-bad-00001",
            items=[DemandItemIn(KEY, ("original",), "c1")],
        ),
        422,
        "supply_conditions_invalid",
    )


# —— 预切合 ——


def prematch_of(w: World, desk: Desk) -> PreMatch:
    return PreMatch(w.repo, desk.repo, w.schedule, global_rate=60, clock=w.clock)


async def test_prematch_answers_from_the_catalog_and_the_queue(db):
    w = World(db)
    desk = Desk(db, w)
    service = prematch_of(w, desk)
    request = PrematchRequestV1.model_validate(
        {
            "items": [
                {"ref": "a", "key": KEY.as_dict(), "readings": ["original", "pages"]},
                {"ref": "b", "key": KEY.as_dict(), "readings": ["text"]},
                {
                    "ref": "c",
                    "key": {"subject": "000858.SZ", "doc_type": "research_company"},
                },
                {"ref": "d", "key": {"subject": "五粮液", "doc_type": "annual_report"}},
                {"ref": "e", "url": "https://www.stats.gov.cn/sj/a.html"},
                # 互动易登记了但还没放行：这类现在取不到
                {"ref": "f", "key": {"subject": "000858.SZ", "doc_type": "ir_qa"}},
                {"ref": "g"},
            ]
        }
    )
    answers = {a.ref: a for a in await service.check("knowbench", request)}
    a = answers["a"]
    assert (a.status, a.license, a.readings_missing) == ("available", "via_expert", [])
    assert [c.channel_id for c in a.candidates] == ["cninfo"]
    assert a.eta_seconds is not None and a.eta_seconds > 0
    assert answers["b"].readings_missing == ["text"]
    assert (answers["c"].status, answers["c"].reason) == ("unavailable", "no_channel")
    assert answers["d"].reason == "subject_unsupported"
    assert (answers["e"].status, answers["e"].reason) == (
        "site_pending",
        "site_unregistered",
    )
    assert (answers["f"].status, answers["f"].reason) == ("unavailable", "no_channel")
    assert answers["g"].reason == "item_invalid"
    # 预切合不建待审网站、不记任何东西
    assert await sql(db, "SELECT count(*) FROM supply_channel WHERE kind = 'web'") == 0
    await desk.catalog.ensure_site("https://www.stats.gov.cn/")
    again = await service.check(
        "knowbench",
        PrematchRequestV1.model_validate(
            {
                "items": [{"ref": "e", "url": "https://www.stats.gov.cn/sj/a.html"}],
                "conditions": {"min_license": "sale"},
            }
        ),
    )
    assert (again[0].status, again[0].reason) == ("site_pending", "site_pending")
    stricter = await service.check(
        "knowbench",
        PrematchRequestV1.model_validate(
            {
                "items": [{"ref": "a", "key": KEY.as_dict()}],
                "conditions": {"min_license": "sale"},
            }
        ),
    )
    assert stricter[0].status == "conditions_unmet"


async def test_prematch_eta_grows_with_the_queue_ahead(db):
    w = World(db)
    desk = Desk(db, w)
    service = prematch_of(w, desk)
    request = PrematchRequestV1.model_validate(
        {"items": [{"ref": "a", "key": KEY.as_dict()}]}
    )
    before = (await service.check("knowbench", request))[0].eta_seconds
    for n in range(2015, 2025):
        await submit(w, "alice", year(n))
    after = (await service.check("knowbench", request))[0].eta_seconds
    assert after > before
    await sql(db, "UPDATE supply_channel SET status = 'paused' WHERE id = 'cninfo'")
    paused = (await service.check("knowbench", request))[0]
    assert (paused.status, paused.eta_seconds) == ("available", None)


async def test_prematch_over_http(db):
    import httpx
    from fastapi import FastAPI

    from app.interfaces.errors.exception_handlers import register_exception_handlers
    from app.interfaces.http.internal import supply as routes

    w = World(db)
    desk = Desk(db, w)
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(routes.router, prefix="/api")
    app.dependency_overrides[routes.internal_host_only] = lambda: None
    app.dependency_overrides[routes.caller_app] = lambda: "knowlab"
    app.dependency_overrides[routes.get_prematch] = lambda: prematch_of(w, desk)
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://knowfeed"
    )
    made = await client.post(
        "/api/internal/v1/supply/prematch",
        json={"items": [{"ref": "c1", "key": KEY.as_dict()}]},
    )
    assert made.status_code == 200, made.text
    body = made.json()
    assert body["contract"] == "knowfeed-supply-prematch/v1"
    assert body["items"][0]["status"] == "available"
    # 坏网址只这一项回 item_invalid，不拒整个请求（独立审第 12 条）
    bad = await client.post(
        "/api/internal/v1/supply/prematch",
        json={
            "items": [
                {"ref": "x", "url": "ftp://example.com/a"},
                {"ref": "c1", "key": KEY.as_dict()},
            ]
        },
    )
    assert bad.status_code == 200, bad.text
    answers = {a["ref"]: a for a in bad.json()["items"]}
    assert (answers["x"]["status"], answers["x"]["reason"]) == (
        "unavailable",
        "item_invalid",
    )
    assert answers["c1"]["status"] == "available"


def test_the_published_prematch_schema_is_generated_from_the_model():
    from app.domain.supply.schema import (
        PREMATCH_DOCUMENTS,
        prematch_contract_dir,
        prematch_manifest,
        render,
    )

    target = prematch_contract_dir()
    if not target.exists():
        pytest.skip("父仓 contracts/ 不在（单独检出后端时）")
    texts = {name: render(name) for name in PREMATCH_DOCUMENTS}
    for name, text in texts.items():
        assert (target / name).read_text(encoding="utf-8") == text, (
            "regenerate: python -m app.domain.supply.schema"
        )
    manifest = (target / "contract-manifest.json").read_text(encoding="utf-8")
    assert manifest == prematch_manifest(texts)
    assert json.loads(manifest)["path"] == "/api/internal/v1/supply/prematch"


# —— 上传资料的加工 ——


class FakeReader:
    def __init__(self, contents: dict[str, tuple[bytes, str]]) -> None:
        self.contents = contents
        self.released: list[str] = []

    async def content(self, grant_id, material_id, version_no):
        return self.contents[material_id]

    async def release(self, grant_id):
        self.released.append(grant_id)


class CoverPages:
    """封面写着证券代码与「2025年年度报告」的 PDF。"""

    def text_pages(self, pdf):
        return [
            (
                1,
                "证券代码：000858 证券简称：五粮液\n宜宾五粮液股份有限公司\n2025年年度报告",
            ),
            (2, "合并资产负债表"),
        ]


async def test_uploaded_materials_are_pulled_queued_processed_and_written_back(db):
    w = World(db)
    reader = FakeReader(
        {
            "m-pdf": (PDF, "application/pdf"),
            "m-md": (
                "# 调研纪要\n\n今天去了工厂。".encode(),
                "text/markdown; charset=utf-8",
            ),
            "m-csv": (b"a,b\n1,2\n", "text/csv; charset=utf-8"),
        }
    )
    w.runner.reader = reader
    w.runner.pdf_pages = CoverPages()
    w.knowbase.pending = [
        ("alice", "m-pdf", 1),
        ("bob", "m-md", 2),
        ("bob", "m-csv", 1),
    ]
    intake = ProcessingIntake(w.knowbase, w.schedule, clock=w.clock)
    assert await intake.pull() == 3
    assert await intake.pull() == 0  # 没办完的不重建
    assert (
        await sql(
            db,
            "SELECT count(*) FROM supply_job WHERE channel_id = :q AND tier = 4",
            q=PROCESS_QUEUE,
        )
        == 3
    )
    picked = await w.dispatch()
    assert len(picked) == 2  # 加工队列同时最多跑两个
    for job_id in picked:
        assert await w.runner.run(job_id) == "done"
    picked = await w.dispatch()
    assert len(picked) == 1
    assert await w.runner.run(picked[0]) == "done"

    calls = w.knowbase.calls
    assert ("grant", "alice") in calls and ("grant", "bob") in calls
    # PDF：按页文字，封面认出是年报全文，抽三张表
    assert ("pages", 2) in calls
    assert any(kind == "facts" for kind, _ in calls)
    # Markdown：正文，头一个一级标题当标题
    text = next(v for k, v in calls if k == "text")
    assert text[1] == "调研纪要" and "工厂" in text[0]
    # CSV：表格整理还没有写回格式，照实记没成
    assert ("failed", "table") in calls
    assert len(reader.released) == 3
    statuses = await sql(
        db, "SELECT string_agg(status, ',') FROM supply_job WHERE kind = 'process'"
    )
    assert statuses == "done,done,done"


async def test_processing_without_the_read_relation_retries_then_fails(db):
    w = World(db)
    w.knowbase.pending = [("alice", "m-1", 1)]
    await ProcessingIntake(w.knowbase, w.schedule, clock=w.clock).pull()
    [job_id] = await w.dispatch()
    assert await w.runner.run(job_id) == "retry"
    assert await sql(db, "SELECT last_error FROM supply_job") == "reader_not_configured"


# —— 互动问答：正文读法、窗里交过的不重取 ——


class QaCatalog(FakeCatalog):
    def __init__(self) -> None:
        super().__init__(
            entries=[
                Entry(
                    "cninfo-irm",
                    "000001:irm:1",
                    "平安银行互动易问答：最近有什么变化",
                    "https://irm.cninfo.com.cn/ircs/question/questionDetail?questionId=1",
                    date(2026, 10, 27),
                    announcement_id="1",
                    doc_type="ir_qa",
                )
            ]
        )
        self.fetched = 0

    async def fetch(self, entry):
        self.fetched += 1
        return "# 平安银行（000001）互动易问答\n\n## 问\n\n…".encode(), (
            "text/markdown; charset=utf-8"
        )


async def test_questions_and_answers_are_delivered_as_text_and_not_fetched_twice(db):
    w = World(db)
    desk = Desk(db, w)
    desk.catalogs["cninfo-irm"] = FakeCatalog()
    # 迁移登记它的时间是建库那一刻：试采要比登记新
    w.clock.now = datetime.now(UTC) + timedelta(minutes=1)
    trial = await desk.catalog.request_trial("cninfo-irm", KEY, actor="op")
    await desk.trials.run(trial.id)
    await desk.catalog.review("cninfo-irm", "approve", actor="op", reason="公开问答")
    qa = QaCatalog()
    w.catalogs["cninfo-irm"] = qa
    window = DescriptionKey("000001.SZ", "ir_qa", date(2026, 10, 1), date(2026, 10, 31))
    # 互动易只有深市公司
    _, sh = await submit(w, "carol", DescriptionKey("600000.SH", "ir_qa"))
    assert (sh[0].status, sh[0].reason) == ("unsatisfiable", "no_channel")

    w.confirm("conf-qa-a", "alice", window)
    d1, items, _ = await w.demands.submit(
        app="knowbench",
        owner="alice",
        proposer="app",
        chain=(),
        task_ref="task-1",
        confirmation_id="conf-qa-a",
        idempotency_key="idem-qa-a-0001",
        items=[DemandItemIn(window, ("original", "text"), "c1")],
    )
    assert items[0].channel_id == "cninfo-irm"
    assert await w.runner.run(await job_of(db, items[0].id)) == "done"
    delivered = w.knowbase.delivered[0]
    assert delivered["name"].endswith(".md") and delivered["doc_type"] == "ir_qa"
    text = next(v for k, v in w.knowbase.calls if k == "text")
    assert text[1:] == ("平安银行互动易问答：最近有什么变化", date(2026, 10, 27))
    _, items = await w.demands.get("knowbench", d1.id)
    assert (items[0].status, items[0].receipt["readings"]) == (
        "ready",
        ["original", "text"],
    )

    w.confirm("conf-qa-b", "bob", window)
    d2, items, _ = await w.demands.submit(
        app="knowbench",
        owner="bob",
        proposer="app",
        chain=(),
        task_ref="task-2",
        confirmation_id="conf-qa-b",
        idempotency_key="idem-qa-b-0001",
        items=[DemandItemIn(window, ("text",), "c1")],
    )
    assert await w.runner.run(await job_of(db, items[0].id)) == "done"
    assert qa.fetched == 1  # 这条问答交过了：只给 bob 记引用
    assert w.knowbase.calls[-1] == ("references", [("bob", f"demand:{d2.id}:c1")])
    _, items = await w.demands.get("knowbench", d2.id)
    assert items[0].status == "ready"
