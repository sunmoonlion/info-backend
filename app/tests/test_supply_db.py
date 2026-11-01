"""供给竖线（SDD 0045 第七节）：精确描述 × 巨潮 × 下载 + 按页文字 + 财报抽表 × 交 knowbase。

财报抽表用真实年报的词坐标夹具（五粮液 2025 年报，期望值与第三方逐项比对过）；
knowbase、巨潮、对象存储换成假的，需求、作业、尝试走真数据库。
"""

from __future__ import annotations

import gzip
import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from test_durable_delivery_db import db as db
from test_durable_delivery_db import sql

from app.application.supply.demands import DemandService
from app.application.supply.jobs import JobRunner
from app.application.supply.processing import FIELD_TO_CODE, statements_from_pages
from app.application.supply.scheduler import Scheduler
from app.domain.securities.report_statements import PageWords, Word
from app.domain.supply.models import DemandItemIn, DescriptionKey, Entry, SupplyError
from app.infrastructure.repositories.supply import SqlSupplyRepository
from app.infrastructure.repositories.supply_schedule import SqlScheduleRepository

WORDS = Path(__file__).parent / "fixtures" / "securities" / "report_words"
EXPECTED = json.loads((WORDS / "expected.json").read_text())
KEY = DescriptionKey("000858.SZ", "annual_report", date(2025, 1, 1), date(2025, 12, 31))
PDF = b"%PDF-1.7 wuliangye-2025"


def pages(name: str) -> list[PageWords]:
    body = json.loads(gzip.decompress((WORDS / f"{name}.json.gz").read_bytes()))
    return [
        PageWords(p["page"], tuple(Word(*w) for w in p["words"])) for p in body["pages"]
    ]


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 27, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


class FakeKnowbase:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []
        self.created = True
        self.fail_deliver: SupplyError | None = None
        self.confirmations: dict[str, dict] = {}
        self.delivered: list[dict] = []
        self.pending: list[tuple[str, str, int]] = []

    async def check_confirmation(self, confirmation_id, subject):
        found = self.confirmations.get(confirmation_id)
        if found is None or found["subject"] != subject:
            raise SupplyError(404, "confirmation_not_found", "no")
        return found

    async def deliver_public(self, **kw):
        if self.fail_deliver:
            raise self.fail_deliver
        self.calls.append(("deliver", kw["channel_doc_id"]))
        self.delivered.append(kw)
        number = len(self.delivered)
        return {"material_id": f"m-{number}", "version_no": 1, "created": self.created}

    async def deliver_collected(self, **kw):
        """为用户采集：每位受益人各得一份自己名下的资料。"""
        if self.fail_deliver:
            raise self.fail_deliver
        self.calls.append(("collected", kw["channel_doc_id"]))
        self.delivered.append(kw)
        number = len(self.delivered)
        return [
            {
                "subject": b["subject"],
                "item_ref": b["item_ref"],
                "material_id": f"m-{number}-{n}",
                "version_no": 1,
                "created": self.created,
            }
            for n, b in enumerate(kw["beneficiaries"], start=1)
        ]

    async def add_references(self, material_id, version_no, references):
        self.calls.append(("references", sorted(references)))
        return len(references)

    async def processing_grant(
        self, material_id, version_no, *, owner="public:disclosure"
    ):
        self.calls.append(("grant", owner))
        return "g-1"

    async def pending_processing(self, limit):
        return list(self.pending)[:limit]

    async def write_text(
        self, grant_id, processor_version, body, *, title, published_on
    ):
        self.calls.append(("text", (body, title, published_on)))
        return {}

    async def write_pages(self, grant_id, processor_version, pages):
        self.calls.append(("pages", len(pages)))
        return {}

    async def write_failed(self, grant_id, kind, processor_version, reason):
        self.calls.append(("failed", kind))
        return {}

    async def write_facts(self, grant_id, processor_version, raw, standard):
        self.calls.append(("facts", (raw, standard)))
        return {}


class FakeCatalog:
    def __init__(self, entries=None) -> None:
        self.entries = (
            entries
            if entries is not None
            else [
                Entry(
                    "cninfo",
                    "000858:annual_report:2025",
                    "五粮液2025年年度报告",
                    "http://static.cninfo.com.cn/finalpage/2026-04-25/1.PDF",
                    date(2026, 4, 25),
                    announcement_id="1",
                    doc_type="annual_report",
                )
            ]
        )

    async def list(self, key):
        return self.entries

    async def fetch(self, entry):
        return PDF + entry.channel_doc_id.encode(), "application/pdf"


class FakeObjects:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}

    async def put(self, key, content, media_type):
        self.data[key] = content

    async def get(self, key):
        return self.data[key]

    async def delete(self, key):
        self.data.pop(key, None)


class FakePages:
    def text_pages(self, pdf):
        return [(1, "五粮液 2025 年年度报告"), (2, "合并资产负债表")]


class FixtureWords:
    def read_words(self, pdf, *, first_page=12):
        return iter(pages("000858-2025"))


class World:
    def __init__(self, db, catalog=None) -> None:
        self.clock = Clock()
        self.repo = SqlSupplyRepository(lambda: db)
        self.knowbase = FakeKnowbase()
        self.objects = FakeObjects()
        self.demands = DemandService(self.repo, self.knowbase, clock=self.clock)
        # 插件按渠道建（生产上给它这条渠道的、经限速的 Http）；这里按插件名换成假的
        self.catalogs = {"cninfo": catalog or FakeCatalog()}
        self.runner = JobRunner(
            self.repo,
            lambda channel: self.catalogs.get(channel.plugin),
            self.knowbase,
            self.objects,
            pdf_pages=FakePages(),
            pdf_words=FixtureWords(),
            clock=self.clock,
        )
        self.schedule = SqlScheduleRepository(lambda: db)
        self.scheduler = Scheduler(self.schedule, process_slots=2, clock=self.clock)

    async def dispatch(self) -> list[str]:
        """调度排一轮（生产上 beat 每 5 秒一次）。"""
        return await self.scheduler.dispatch()

    def confirm(
        self,
        confirmation_id: str,
        subject: str,
        *keys: DescriptionKey,
        supply: dict | None = None,
    ):
        self.knowbase.confirmations[confirmation_id] = {
            "subject": subject,
            "items": [
                {"item_id": f"c{n + 1}", "key": k.as_dict()} for n, k in enumerate(keys)
            ],
            "supply": supply or {},
        }

    async def submit(
        self,
        owner="alice",
        confirmation="conf-1",
        key=KEY,
        idem="idem-0001-abcd",
        readings=("original", "pages", "table"),
    ):
        return await self.demands.submit(
            app="knowbench",
            owner=owner,
            proposer="app",
            chain=("expert:fa-1@2", "task-1"),
            task_ref="task-1",
            confirmation_id=confirmation,
            idempotency_key=idem,
            items=[DemandItemIn(key, readings, "c1")],
        )


async def refused(work, status: int, code: str) -> None:
    with pytest.raises(SupplyError) as caught:
        await work
    assert (caught.value.status, caught.value.code) == (status, code)


def test_annual_statements_become_raw_and_standard_facts():
    facts = statements_from_pages(pages("000858-2025"), KEY)
    # 夹具只裁了报表页，附注里的补充资料不在里面
    assert facts.problems == ["supplement_not_found"]
    by_code = {
        (s["code"], s["period_end"]): Decimal(s["value"]) for s in facts.standard
    }
    expected = EXPECTED["000858-2025"]["statements"]
    for statement, field, value in (
        ("balance_sheet", "total_assets", "189984270815.47"),
        ("income_statement", "netprofit", "9317113184.68"),
        ("cash_flow", "end_cce", "124114690633.67"),
    ):
        assert expected[statement]["values"]["current"][field] == value
        assert by_code[(FIELD_TO_CODE[field], "2025-12-31")] == Decimal(value)
    # 每个标准化的数都能追到原样层那一行，页码一致
    for s in facts.standard:
        raw = facts.raw[s["derived_from"][0]]
        assert raw["page"] == s["page"] and raw["period_end"] == s["period_end"]
    assert {r["statement_label"] for r in facts.raw} == {
        "合并资产负债表",
        "合并利润表",
        "合并现金流量表",
    }
    # 上期列是上年同期
    assert ("BS.TOTAL_ASSETS", "2024-12-31") in by_code


def test_scanned_statements_give_no_numbers():
    scanned = statements_from_pages(
        pages("601899-2025"),
        DescriptionKey(
            "601899.SH", "annual_report", date(2025, 1, 1), date(2025, 12, 31)
        ),
    )
    # 报表是图片：一个数也不给，照实写原因
    assert scanned.raw == [] and scanned.standard == []
    assert scanned.problems


async def test_demand_intake_checks_confirmation_and_idempotency(db):
    w = World(db)
    await refused(w.submit(), 404, "confirmation_not_found")
    w.confirm("conf-1", "alice", KEY)
    demand, items, replayed = await w.submit()
    assert not replayed and [i.status for i in items] == ["fetching"]
    again, _, replayed = await w.submit()
    assert replayed and again.id == demand.id
    await refused(w.submit(readings=("original",)), 409, "idempotency_key_reused")
    other = DescriptionKey(
        "000858.SZ", "annual_report", date(2024, 1, 1), date(2024, 12, 31)
    )
    await refused(
        w.submit(key=other, idem="idem-0002-abcd"), 403, "outside_confirmation"
    )
    # 来源目录里没有能供这一类的渠道：无法满足，原因是「这类数据还取不到」（进缺口清单）
    w.confirm("conf-2", "alice", DescriptionKey("000858.SZ", "announcement"))
    _, items, _ = await w.submit(
        confirmation="conf-2",
        key=DescriptionKey("000858.SZ", "announcement"),
        idem="idem-0003-abcd",
    )
    assert [(i.status, i.reason) for i in items] == [("unsatisfiable", "no_channel")]


async def test_one_shared_job_delivers_to_knowbase_and_settles_every_waiting_item(db):
    w = World(db)
    w.confirm("conf-a", "alice", KEY)
    w.confirm("conf-b", "bob", KEY)
    a, _, _ = await w.submit(owner="alice", confirmation="conf-a")
    b, _, _ = await w.submit(
        owner="bob", confirmation="conf-b", idem="idem-bob-0001", readings=("original",)
    )
    jobs = await sql(db, "SELECT count(*) FROM supply_job")
    assert jobs == 1  # 同一渠道同一描述跨用户只取一次
    job_id = str(await sql(db, "SELECT id FROM supply_job"))
    assert await w.runner.run(job_id) == "done"

    kinds = [c[0] for c in w.knowbase.calls]
    assert kinds == ["deliver", "grant", "pages", "facts", "references"]
    refs = dict(w.knowbase.calls)["references"]
    assert [s for s, _ in refs] == ["alice", "bob"]
    raw, standard = dict(w.knowbase.calls)["facts"]
    assert any(s["code"] == "BS.TOTAL_ASSETS" for s in standard) and raw
    _, a_items = await w.demands.get("knowbench", a.id)
    assert [(i.status, i.receipt["readings"]) for i in a_items] == [
        ("ready", ["original", "pages", "table"])
    ]
    _, b_items = await w.demands.get("knowbench", b.id)
    assert b_items[0].status == "ready" and b_items[0].receipt["material_id"] == "m-1"
    assert await sql(db, "SELECT count(*) FROM supply_original") == 1
    assert (
        await sql(db, "SELECT knowbase_material_id FROM supply_original_delivery")
        == "m-1"
    )
    assert await sql(db, "SELECT status FROM supply_job") == "done"
    # 跑过的作业再投一次什么也不做
    assert await w.runner.run(job_id) == "skipped"


async def test_absent_documents_temporary_errors_and_late_events(db):
    empty = World(db, catalog=FakeCatalog(entries=[]))
    empty.confirm("conf-1", "alice", KEY)
    demand, _, _ = await empty.submit()
    job_id = str(await sql(db, "SELECT id FROM supply_job"))
    assert await empty.runner.run(job_id) == "absent"
    _, items = await empty.demands.get("knowbench", demand.id)
    assert items[0].status == "unsatisfiable"

    flaky = World(db)
    flaky.confirm("conf-9", "carol", KEY)
    d9, _, _ = await flaky.submit(
        owner="carol", confirmation="conf-9", idem="idem-0009-abcd"
    )
    job9 = str(await sql(db, "SELECT id FROM supply_job WHERE status = 'queued'"))
    flaky.knowbase.fail_deliver = SupplyError(503, "knowbase_error", "down")
    assert await flaky.runner.run(job9) == "retry"
    assert (
        await sql(db, "SELECT status FROM supply_job WHERE id = :id", id=job9)
        == "waiting"
    )
    # 用户取消以后，迟到的完成不能把它改成已就绪
    await flaky.demands.cancel("knowbench", d9.id)
    flaky.knowbase.fail_deliver = None
    assert await flaky.runner.run(job9) == "done"
    _, items = await flaky.demands.get("knowbench", d9.id)
    assert items[0].status == "cancelled"
    await refused(flaky.demands.get("knowlab", d9.id), 404, "demand_not_found")


async def test_apps_submit_follow_and_cancel_demands_over_http(db):
    import httpx
    from fastapi import FastAPI

    from app.interfaces.errors.exception_handlers import register_exception_handlers
    from app.interfaces.http.internal import supply as routes

    w = World(db)
    w.confirm("conf-1", "alice", KEY)
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(routes.router, prefix="/api")
    app.dependency_overrides[routes.internal_host_only] = lambda: None
    app.dependency_overrides[routes.caller_app] = lambda: "knowbench"
    app.dependency_overrides[routes.get_demand_service] = lambda: w.demands
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://knowfeed"
    )
    body = {
        "owner": "alice",
        "task_ref": "task-1",
        "confirmation_id": "conf-1",
        "idempotency_key": "idem-http-0001",
        "items": [
            {
                "confirmation_item": "c1",
                "key": KEY.as_dict(),
                "readings": ["original", "table"],
            }
        ],
    }
    made = await client.post("/api/internal/v1/supply/demands", json=body)
    assert made.status_code == 201, made.text
    assert (made.json()["status"], made.json()["items"][0]["status"]) == (
        "fetching",
        "fetching",
    )
    demand_id = made.json()["demand_id"]
    again = await client.post("/api/internal/v1/supply/demands", json=body)
    assert again.json()["replayed"] is True
    seen = await client.get(f"/api/internal/v1/supply/demands/{demand_id}")
    assert seen.json()["demand_id"] == demand_id
    cancelled = await client.post(f"/api/internal/v1/supply/demands/{demand_id}/cancel")
    assert cancelled.json()["status"] == "cancelled"
    missing = await client.get("/api/internal/v1/supply/demands/nope")
    assert (missing.status_code, missing.json()["code"]) == (404, "demand_not_found")


async def test_a_disclosure_window_delivers_each_announcement_as_its_own_material(db):
    """SDD 0049：非定期报告一条描述是一个披露时间窗，窗里的逐份交付；描述按插件认出的
    细类、版本形态、事项写，不带报告期，不抽三大表。"""
    window = DescriptionKey(
        "000858.SZ",
        "interim_announcement",
        date(2026, 1, 1),
        date(2026, 10, 1),
        topics=("equity",),
    )
    entries = [
        Entry(
            "cninfo",
            f"000858:announcement:{n}",
            title,
            f"http://static.cninfo.com.cn/finalpage/2026-0{n}-01/{n}.PDF",
            date(2026, n, 1),
            announcement_id=str(n),
            doc_type="interim_announcement",
            topics=("equity",),
        )
        for n, title in ((5, "关于回购股份的公告"), (3, "关于股东减持计划的公告"))
    ]
    w = World(db, catalog=FakeCatalog(entries=entries))
    w.confirm("conf-w", "alice", window)
    demand, _, _ = await w.submit(
        confirmation="conf-w", key=window, idem="idem-window-01", readings=("pages",)
    )
    job_id = str(await sql(db, "SELECT id FROM supply_job"))
    assert await w.runner.run(job_id) == "done"
    kinds = [c[0] for c in w.knowbase.calls]
    assert kinds == ["deliver", "grant", "pages", "references"] * 2  # 不抽表
    first = w.knowbase.delivered[0]
    assert (first["doc_type"], first["topics"], first["edition"]) == (
        "interim_announcement",
        ("equity",),
        "full",
    )
    assert first["period_start"] is None and first["period_end"] is None
    _, items = await w.demands.get("knowbench", demand.id)
    assert items[0].status == "ready"
    assert [m["material_id"] for m in items[0].receipt["materials"]] == ["m-1", "m-2"]
    assert await sql(db, "SELECT count(*) FROM supply_original") == 2
