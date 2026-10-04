"""给网页端的预览造样例（info-web-frontend `preview/fixtures/`）。

各种进度由真的申请服务、真的库、真的接口造出来；采集与建库走到哪一步，
是往采集批次、数据集这两张表里直接写出来的（和接口的测试同一个做法）。
平时它就是一个测试。要把样例写进网页端的仓库：

    PREVIEW_FIXTURES_OUT=<网页端>/app/preview/fixtures uv run pytest tests/test_preview_fixtures.py
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi import FastAPI, Request
from preview_recorder import Recorder
from security_requests_support import ALICE, BOB, OWNER
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker
from test_durable_delivery_db import db as db  # noqa: F401
from test_security_dataset_db import MemoryStorage
from test_security_request_routes_db import principal

from app.bootstrap import securities as wiring
from app.bootstrap import security_requests as request_wiring
from app.domain.security import Principal
from app.infrastructure.storage.postgres import get_db_session
from app.interfaces.http.admin import security_requests as admin_routes
from app.interfaces.http.middleware.auth import (
    get_web_current_user,
    require_info_admin,
)
from app.interfaces.http.web import cross_app as cross_app_routes
from app.interfaces.http.web import security_requests as web_routes
from core.config import Settings

NEWCOMER = "00000000-0000-4000-8000-00000000000c"


@pytest.fixture
async def served(db, monkeypatch):  # noqa: F811
    sessions = async_sessionmaker(db.kw["bind"], autocommit=False, autoflush=False)
    config = Settings(
        _env_file=None,
        cross_app_sources_json=(
            '{"investment": {"return_url":'
            ' "http://localhost:3100/zh-CN/workbench?ref={ref}"}, "knowledge": {}}'
        ),
        cross_app_targets_json=(
            '{"knowledge": {"web_base_url": "http://localhost:3120"}}'
        ),
        # 打开「向知识服务登记」：这样才有「登记中」「登记失败」这两种进度
        KNOWLEDGE_APP_DATASET_URL="http://knowledge.invalid/api/internal/datasets",
        KNOWLEDGE_APP_SERVICE_CLIENT_ID="preview",
        KNOWLEDGE_APP_SERVICE_CLIENT_SECRET="preview-not-a-secret",
    )
    assert config.knowledge_app_dataset_enabled
    who = {"actor": ALICE}

    async def session():
        async with sessions() as s:
            yield s

    async def web_user(request: Request) -> Principal:
        return principal(who["actor"])

    async def admin_user() -> Principal:
        return principal(OWNER, "admin")

    monkeypatch.setattr(wiring, "get_object_storage", MemoryStorage)
    for module in (web_routes, admin_routes):
        monkeypatch.setattr(
            module, "get_postgres", lambda: SimpleNamespace(session_factory=sessions)
        )
        monkeypatch.setattr(module, "get_settings", lambda: config)
    monkeypatch.setattr(request_wiring, "get_settings", lambda: config)
    app = FastAPI()
    app.include_router(web_routes.router, prefix="/api")
    app.include_router(admin_routes.router, prefix="/api")
    app.include_router(cross_app_routes.router, prefix="/api")
    app.dependency_overrides[cross_app_routes.cross_app_settings] = lambda: config
    app.dependency_overrides[get_db_session] = session
    app.dependency_overrides[get_web_current_user] = web_user
    app.dependency_overrides[require_info_admin] = admin_user
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://info"
    ) as client:
        yield SimpleNamespace(http=client, db=db, who=who)


class World:
    def __init__(self, served, recorder: Recorder) -> None:
        self.http, self.db, self.who, self.rec = (
            served.http,
            served.db,
            served.who,
            recorder,
        )
        self.requests: dict[str, str] = {}

    async def sql(self, statement: str, **values: Any) -> None:
        async with self.db() as s, s.begin():
            await s.execute(text(statement), values)

    async def submit(
        self, name: str, code: str, *, by: str = ALICE, **body: Any
    ) -> dict:
        self.who["actor"] = by
        made = await self.rec.call(
            self.http,
            "POST",
            "/api/web/v1/security-requests",
            expect=201,
            json_body={"security_code": code, **body},
        )
        self.who["actor"] = ALICE
        self.requests[name] = made["id"]
        return made

    async def approve(self, name: str) -> str:
        done = await self.http.post(
            f"/api/admin/security-requests/{self.requests[name]}/approval"
        )
        assert done.status_code == 200, done.text
        return done.json()["ingestion_id"]

    async def collected(self, ingestion: str) -> None:
        await self.sql(
            "UPDATE security_ingestion SET status='succeeded', finished_at=now()"
            " WHERE id = cast(:i as uuid)",
            i=ingestion,
        )

    async def built(
        self,
        ingestion: str,
        code: str,
        market: str,
        *,
        status: str = "published",
        registered: bool = True,
        error: str | None = None,
    ) -> None:
        dataset = f"{market}{code}-financials"
        await self.sql(
            "INSERT INTO security_dataset (security_code, dataset_id, data_version,"
            " status, ingestion_id, bucket, object_key, sha256, size_bytes,"
            " start_date, end_date, built_at, knowledge_registered_at,"
            " knowledge_registration_error) VALUES (:code, :dataset, :version, :status,"
            " cast(:i as uuid), 'preview-bucket', :key, :sha, 4096, '2016-12-31',"
            " '2025-12-31', now(), "
            + ("now()" if registered else "null")
            + ", :error)",
            code=code,
            dataset=dataset,
            version=f"{dataset}-sample{code}",
            status=status,
            i=ingestion,
            key=f"datasets/{dataset}.sqlite",
            sha=(code * 11)[:64].ljust(64, "0"),
            error=error,
        )

    async def see(self, name: str) -> dict:
        return await self.rec.get(
            self.http, f"/api/web/v1/security-requests/{self.requests[name]}"
        )


async def build_full(world: World) -> None:
    rec, http = world.rec, world.http

    # 已经结束的
    done = await world.submit("available", "002594", reason="想看比亚迪近三年的毛利率")
    assert done["progress"] == "pending"
    batch = await world.approve("available")
    await world.collected(batch)
    await world.built(batch, "002594", "sz")
    assert (await world.see("available"))["progress"] == "available"

    await world.submit("quality-failed", "600436")
    batch = await world.approve("quality-failed")
    await world.collected(batch)
    await world.built(batch, "600436", "sh", status="quality_failed", registered=False)
    assert (await world.see("quality-failed"))["progress"] == "quality_failed"

    await world.submit("failed", "601318")
    batch = await world.approve("failed")
    await world.sql(
        "UPDATE security_ingestion SET status='failed', error_code='source_unreachable',"
        " finished_at=now() WHERE id = cast(:i as uuid)",
        i=batch,
    )
    assert (await world.see("failed"))["progress"] == "failed"

    await world.submit("registration-failed", "000333")
    batch = await world.approve("registration-failed")
    await world.collected(batch)
    await world.built(
        batch, "000333", "sz", registered=False, error="knowledge_unavailable"
    )
    assert (await world.see("registration-failed"))["progress"] == "registration_failed"

    await world.submit(
        "rejected", "688981", reason="看看", **{"from": "investment", "ref": "task:42"}
    )
    refused = await http.post(
        f"/api/admin/security-requests/{world.requests['rejected']}/rejection",
        json={"note": "科创板的公司暂时不采：年报的格式还没有适配"},
    )
    assert refused.status_code == 200, refused.text
    assert (await world.see("rejected"))["progress"] == "rejected"

    await world.submit("withdrawn", "600030", reason="填错了代码")
    await rec.call(
        http,
        "POST",
        f"/api/web/v1/security-requests/{world.requests['withdrawn']}/withdrawal",
        expect=200,
    )
    assert (await world.see("withdrawn"))["progress"] == "withdrawn"

    # 还在走的。一个人同时最多五个，这里正好五个
    await world.submit(
        "pending",
        "601012",
        reason="要做光伏行业的对比",
        **{"from": "investment", "ref": "task:7f3a"},
    )
    # 别人也申请了同一家：并到同一个申请里
    await world.submit("pending-joined", "601012", by=BOB, reason="同上")
    assert world.requests["pending-joined"] == world.requests["pending"]

    await world.submit("queued", "600036")
    await world.approve("queued")
    assert (await world.see("queued"))["progress"] == "queued"

    await world.submit("collecting", "000651")
    batch = await world.approve("collecting")
    await world.sql(
        "UPDATE security_ingestion SET status='running', started_at=now()"
        " WHERE id = cast(:i as uuid)",
        i=batch,
    )
    assert (await world.see("collecting"))["progress"] == "collecting"

    await world.submit("building", "600887")
    batch = await world.approve("building")
    await world.collected(batch)
    assert (await world.see("building"))["progress"] == "building"

    await world.submit("registering", "601166")
    batch = await world.approve("registering")
    await world.collected(batch)
    await world.built(batch, "601166", "sh", registered=False)
    assert (await world.see("registering"))["progress"] == "registering"

    # 第六个：超过了同时未完成的上限
    await rec.call(
        http,
        "POST",
        "/api/web/v1/security-requests",
        expect=409,
        json_body={"security_code": "600585"},
    )

    await rec.get(http, "/api/web/v1/security-requests")
    for code in (
        "002594",  # 有数据
        "601012",  # 我申请过，等批准
        "600519",  # 在关注清单里，还没有数据
        "600585",  # 没有数据，也没人申请过
        "600436",  # 采到了，没通过质量检查
    ):
        await rec.get(http, f"/api/web/v1/securities/{code}/availability")
    await rec.get(http, "/api/web/v1/securities/12345/availability", expect=422)

    rec.page("我的申请", "/zh-CN/requests")
    rec.page("申请入库（什么都没带）", "/zh-CN/requests/new")
    rec.page(
        "申请入库（从 investment 带着代码过来）",
        "/zh-CN/requests/new?code=600585&from=investment&ref=task:42",
    )
    rec.page(
        "申请入库（这家已经有数据）", "/zh-CN/requests/new?code=002594&from=knowledge"
    )
    rec.page("申请入库（我已经申请过）", "/zh-CN/requests/new?code=601012")
    rec.page("申请入库（代码不对）", "/zh-CN/requests/new?code=12345")
    for title, name in (
        ("一个申请：等批准", "pending"),
        ("一个申请：排队中", "queued"),
        ("一个申请：采集中", "collecting"),
        ("一个申请：建库中", "building"),
        ("一个申请：登记中", "registering"),
        ("一个申请：可用", "available"),
        ("一个申请：没通过质量检查", "quality-failed"),
        ("一个申请：采集失败", "failed"),
        ("一个申请：登记失败", "registration-failed"),
        ("一个申请：被拒绝", "rejected"),
        ("一个申请：已撤回", "withdrawn"),
    ):
        # 这一页要取的那个申请也录下来。漏过「等批准」这一个：预览里那一页打不开
        await rec.get(http, f"/api/web/v1/security-requests/{world.requests[name]}")
        rec.page(title, f"/zh-CN/requests/{world.requests[name]}")


async def build_empty(world: World) -> None:
    world.who["actor"] = NEWCOMER
    await world.rec.get(world.http, "/api/web/v1/security-requests")
    await world.rec.get(world.http, "/api/web/v1/securities/600585/availability")
    # 提交之后的样子：在预览里点「提交」，得到的就是这一个
    await world.submit("first", "600585", by=NEWCOMER, reason="想看海螺水泥")
    world.who["actor"] = NEWCOMER
    await world.see("first")
    world.rec.page("我的申请（一个都没有）", "/zh-CN/requests")
    world.rec.page("刚提交的申请", f"/zh-CN/requests/{world.requests['first']}")
    world.rec.page("申请入库", "/zh-CN/requests/new?code=600585")


SCENARIOS = {
    "full": (
        "什么都有",
        "一个申请过十一家公司的人：每种进度各一个，其中五个还在走（同时未完成的上限就是五个）。",
        build_full,
    ),
    "empty": ("刚来", "一个申请都没有。", build_empty),
}


def where_to(tmp_path: Path) -> Path:
    wanted = os.environ.get("PREVIEW_FIXTURES_OUT")
    return Path(wanted) if wanted else tmp_path


async def record(scenario: str, served, out: Path) -> tuple[Path, dict[str, Any]]:
    title, description, build = SCENARIOS[scenario]
    recorder = Recorder(scenario, title=title, description=description)
    await build(World(served, recorder))
    directory = recorder.write(out)
    manifest = json.loads((directory / "manifest.json").read_text())
    for response in manifest["responses"]:
        body = (directory / response["file"]).read_text()
        # 对象存储的位置、别人的身份不在样例里
        assert "preview-bucket" not in body and "datasets/" not in body
        assert "preview-not-a-secret" not in body
    return directory, manifest


async def test_the_full_world(served, tmp_path):
    directory, manifest = await record("full", served, where_to(tmp_path))
    listed = next(
        r
        for r in manifest["responses"]
        if r["method"] == "GET" and r["path"] == "/api/web/v1/security-requests"
    )
    mine = json.loads((directory / listed["file"]).read_text())
    assert sorted(r["progress"] for r in mine) == sorted(
        [
            "available",
            "building",
            "collecting",
            "failed",
            "pending",
            "quality_failed",
            "queued",
            "registering",
            "registration_failed",
            "rejected",
            "withdrawn",
        ]
    )
    assert len(manifest["pages"]) == 17
    # 每一个「一个申请」的页，它要取的那个申请都有样例
    sampled = {r["path"] for r in manifest["responses"] if r["method"] == "GET"}
    for page in manifest["pages"]:
        request = page["path"].removeprefix("/zh-CN/requests/")
        if "/" not in request and "?" not in request and request not in ("", "new"):
            if page["path"] != "/zh-CN/requests":
                assert f"/api/web/v1/security-requests/{request}" in sampled, page
    refused = [r for r in manifest["responses"] if r["status"] == 409]
    assert len(refused) == 1


async def test_a_newcomer(served, tmp_path):
    directory, manifest = await record("empty", served, where_to(tmp_path))
    listed = json.loads((directory / manifest["responses"][0]["file"]).read_text())
    assert listed == []
    made = [r for r in manifest["responses"] if r["method"] == "POST"]
    assert [(r["path"], r["status"]) for r in made] == [
        ("/api/web/v1/security-requests", 201)
    ]
