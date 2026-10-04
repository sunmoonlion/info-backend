"""采集申请的接口：真数据库；用户面与管理面；链接带来的参数；谁能看到什么。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, Request
from security_requests_support import ALICE, BOB, OWNER
from sqlalchemy import text
from test_auth_routes_security import FakeAuthService
from test_auth_routes_security import session as browser_session
from test_durable_delivery_db import db as db
from test_durable_delivery_db import sql
from test_security_dataset_db import MemoryStorage

import app.interfaces.http.middleware.auth as auth_middleware
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
from app.main import app as real_app
from core.config import Settings

SOURCES = (
    '{"investment": {"return_url": '
    '"https://investment.example.test/zh-CN/workbench/back?ref={ref}"},'
    ' "knowledge": {}}'
)


def principal(actor: str, surface: str = "web") -> Principal:
    now = datetime.now(UTC)
    return Principal(
        actor_type="user",
        subject="s-" + actor[-2:],
        issuer="https://identity.example.test",
        app="info",
        surface=surface,  # type: ignore[arg-type]
        audience=f"{surface}-client",
        actor_id=uuid.UUID(actor),
        roles=(),
        scopes=frozenset(),
        authenticated_at=now,
        expires_at=now + timedelta(minutes=10),
        policy_version="1",
    )


@pytest.fixture
async def api(db, monkeypatch):
    sessions = db
    config = Settings(
        _env_file=None,
        cross_app_sources_json=SOURCES,
        cross_app_targets_json=(
            '{"knowledge": {"web_base_url": "https://knowledge.example.test"}}'
        ),
    )

    async def session():
        async with sessions() as s:
            yield s

    async def web_user(request: Request) -> Principal:
        return principal(request.headers["x-test-actor"])

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
        yield SimpleNamespace(client=client, db=db)


def as_(actor: str) -> dict[str, str]:
    return {"x-test-actor": actor}


async def submit(api, actor, code="000001", **body):
    return await api.client.post(
        "/api/web/v1/security-requests",
        json={"security_code": code, **body},
        headers=as_(actor),
    )


# ---------------------------------------------------------------- 用户面
async def test_submit_outside_the_watchlist_then_see_it_in_my_requests(api):
    """AT-INFO-01"""
    before = await api.client.get(
        "/api/web/v1/securities/000001/availability", headers=as_(ALICE)
    )
    assert before.status_code == 200
    assert before.json() == {
        "security_code": "000001",
        "market": "SZ",
        "in_watchlist": False,
        "dataset": None,
        "open_request": None,
        "mine": False,
        "my_open_count": 0,
        "max_open": 5,
        "needs_approval": True,
    }
    created = await submit(
        api, ALICE, reason=" 要看净息差 ", **{"from": "investment", "ref": "task:42"}
    )
    assert created.status_code == 201
    body = created.json()
    assert body["progress"] == "pending" and body["kind"] == "initial"
    assert body["requesters"] == 1 and body["can_withdraw"] is True
    assert body["rejection_note"] is None and body["dataset"] is None
    assert body["mine"]["reason"] == "要看净息差"
    assert body["mine"]["origin"] == {
        "app": "investment",
        "ref": "task:42",
        "return_url": "https://investment.example.test/zh-CN/workbench/back?ref=task%3A42",
    }
    assert await sql(api.db, "SELECT count(*) FROM security_ingestion") == 0
    listed = await api.client.get("/api/web/v1/security-requests", headers=as_(ALICE))
    assert [r["id"] for r in listed.json()] == [body["id"]]
    after = await api.client.get(
        "/api/web/v1/securities/000001/availability", headers=as_(ALICE)
    )
    assert after.json()["mine"] is True and after.json()["my_open_count"] == 1
    assert after.json()["open_request"]["id"] == body["id"]


async def test_a_user_sees_nothing_about_other_people(api):
    """AT-INFO-16、F-INFO-29"""
    mine = (await submit(api, ALICE, reason="甲的理由")).json()
    joined = await submit(api, BOB, reason="乙的理由")
    assert joined.status_code == 201 and joined.json()["id"] == mine["id"]
    seen = joined.json()
    assert seen["requesters"] == 2 and seen["mine"]["reason"] == "乙的理由"
    assert "甲的理由" not in joined.text and ALICE not in joined.text
    assert "decided_by" not in seen and "status" not in seen

    other = (await submit(api, ALICE, code="000002")).json()
    for method, path in [
        ("GET", f"/api/web/v1/security-requests/{other['id']}"),
        ("POST", f"/api/web/v1/security-requests/{other['id']}/withdrawal"),
    ]:
        response = await api.client.request(method, path, headers=as_(BOB))
        assert response.status_code == 404, path
        assert response.json()["detail"]["code"] == "request_not_found"
    listed = await api.client.get("/api/web/v1/security-requests", headers=as_(BOB))
    assert [r["id"] for r in listed.json()] == [mine["id"]]


@pytest.mark.parametrize(
    ("params", "expected"),
    [
        ({"from": "investment", "ref": "task:42"}, ("investment", "task:42", True)),
        ({"from": "knowledge", "ref": "x"}, ("knowledge", "x", False)),
        ({"from": "investment", "ref": "bad ref"}, ("investment", None, True)),
        ({"from": "investment"}, ("investment", None, True)),
        ({"from": "unknown", "ref": "x"}, None),
        ({"ref": "x"}, None),
        ({}, None),
    ],
)
async def test_origin_is_resolved_from_configuration_only(api, params, expected):
    """AT-INFO-12、F-INFO-32"""
    response = await api.client.get(
        "/api/web/v1/cross-app/origin", params=params, headers=as_(ALICE)
    )
    assert response.status_code == 200
    body = response.json()
    if expected is None:
        assert body is None
        return
    app, ref, has_return = expected
    assert (body["app"], body["ref"]) == (app, ref)
    assert (body["return_url"] is not None) is has_return
    if has_return:
        assert body["return_url"].startswith("https://investment.example.test/")


async def test_a_return_address_in_the_link_is_ignored(api):
    """AT-INFO-13"""
    response = await api.client.get(
        "/api/web/v1/cross-app/origin",
        params={
            "from": "investment",
            "ref": "t1",
            "return_to": "https://evil.example.test/",
            "return_url": "https://evil.example.test/",
        },
        headers=as_(ALICE),
    )
    assert "evil" not in response.text
    refused = await api.client.post(
        "/api/web/v1/security-requests",
        json={"security_code": "000001", "return_url": "https://evil.example.test/"},
        headers=as_(ALICE),
    )
    assert refused.status_code == 422  # 不认识的字段一律拒收


@pytest.mark.parametrize("code", ["60000", "abc123", "900901", "6000090"])
async def test_codes_that_are_not_a_share_codes_are_refused(api, code):
    """AT-INFO-11 的后端一半：页面照常打开，接口拒绝这个代码。"""
    response = await submit(api, ALICE, code=code)
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "security_code_invalid"
    looked = await api.client.get(
        f"/api/web/v1/securities/{code}/availability", headers=as_(ALICE)
    )
    assert looked.status_code == 422
    assert await sql(api.db, "SELECT count(*) FROM security_request") == 0


async def test_limit_and_withdrawal(api):
    """AT-INFO-07、AT-INFO-08"""
    ids = []
    for code in ("000001", "000002", "000004", "000006", "000007"):
        ids.append((await submit(api, ALICE, code=code)).json()["id"])
    full = await submit(api, ALICE, code="000008")
    assert full.status_code == 409
    assert full.json()["detail"]["code"] == "too_many_open_requests"
    withdrawn = await api.client.post(
        f"/api/web/v1/security-requests/{ids[0]}/withdrawal", headers=as_(ALICE)
    )
    assert withdrawn.status_code == 200
    assert withdrawn.json()["progress"] == "withdrawn"
    assert withdrawn.json()["can_withdraw"] is False
    assert (await submit(api, ALICE, code="000008")).status_code == 201
    pending = await api.client.get(
        "/api/admin/security-requests", params={"status": "pending", "open": "true"}
    )
    assert ids[0] not in [r["id"] for r in pending.json()]


async def test_a_watched_company_is_queued_at_once(api):
    """AT-INFO-03"""
    looked = await api.client.get(
        "/api/web/v1/securities/600519/availability", headers=as_(ALICE)
    )
    assert looked.json()["needs_approval"] is False
    created = await submit(api, ALICE, code="600519")
    assert created.json()["progress"] == "queued"
    assert created.json()["can_withdraw"] is False
    assert await sql(api.db, "SELECT count(*) FROM security_ingestion") == 1
    admin = await api.client.get("/api/admin/security-requests")
    assert admin.json()[0]["decided_by"] == "system:watchlist"
    assert admin.json()[0]["in_watchlist"] is True


# ---------------------------------------------------------------- 管理面
async def test_owner_approves_and_the_user_sees_the_data_arrive(api):
    """AT-INFO-02"""
    request_id = (await submit(api, ALICE, reason="要看")).json()["id"]
    pending = await api.client.get(
        "/api/admin/security-requests", params={"status": "pending", "open": "true"}
    )
    row = pending.json()[0]
    assert row["id"] == request_id and row["status"] == "pending"
    assert row["requesters"][0]["actor_id"] == ALICE
    assert row["requesters"][0]["reason"] == "要看"
    approved = await api.client.post(
        f"/api/admin/security-requests/{request_id}/approval"
    )
    assert approved.status_code == 200
    body = approved.json()
    assert (body["status"], body["progress"]) == ("approved", "queued")
    assert body["decided_by"] == OWNER and body["ingestion_id"]
    assert (
        await sql(
            api.db,
            "SELECT aggregate_key FROM outbox_message "
            "WHERE topic='info.security.ingest.v1'",
        )
        == body["ingestion_id"]
    )
    async with api.db() as s, s.begin():
        await s.execute(text("UPDATE security_ingestion SET status='succeeded'"))
        await s.execute(
            text(
                "INSERT INTO security_dataset (security_code, dataset_id, data_version,"
                " status, ingestion_id, bucket, object_key, sha256, size_bytes,"
                " start_date, end_date, built_at, knowledge_registered_at) VALUES"
                " ('000001', 'sz000001-financials', 'sz000001-financials-aaaa',"
                " 'published', :i, 'secret-bucket', 'secret/key', :sha, 1,"
                " '2016-12-31', '2025-12-31', now(), now())"
            ),
            {"i": body["ingestion_id"], "sha": "a" * 64},
        )
    seen = await api.client.get(
        f"/api/web/v1/security-requests/{request_id}", headers=as_(ALICE)
    )
    assert seen.json()["progress"] == "available"
    assert seen.json()["dataset"] == {
        "dataset_id": "sz000001-financials",
        "data_version": "sz000001-financials-aaaa",
        "start_date": "2016-12-31",
        "end_date": "2025-12-31",
    }
    # 去 knowledge 看这个数据集的链接由网页端拼，后端不给地址
    assert "knowledge.example.test" not in seen.text
    assert "secret" not in seen.text  # 对象存储的位置不给用户
    now = await api.client.get(
        "/api/web/v1/securities/000001/availability", headers=as_(BOB)
    )
    assert now.json()["dataset"]["end_date"] == "2025-12-31"
    assert now.json()["open_request"] is None


async def test_rejection_shows_the_reason_but_not_who(api):
    """AT-INFO-04、F-INFO-28"""
    request_id = (await submit(api, ALICE)).json()["id"]
    for note in ("", "   "):
        empty = await api.client.post(
            f"/api/admin/security-requests/{request_id}/rejection", json={"note": note}
        )
        assert empty.status_code == 422
        assert empty.json()["detail"]["code"] == "rejection_note_required"
    rejected = await api.client.post(
        f"/api/admin/security-requests/{request_id}/rejection",
        json={"note": "存储不够，下个月再说"},
    )
    assert rejected.status_code == 200 and rejected.json()["decided_by"] == OWNER
    seen = await api.client.get(
        f"/api/web/v1/security-requests/{request_id}", headers=as_(ALICE)
    )
    assert seen.json()["progress"] == "rejected"
    assert seen.json()["rejection_note"] == "存储不够，下个月再说"
    assert OWNER not in seen.text
    again = await api.client.post(f"/api/admin/security-requests/{request_id}/approval")
    assert again.status_code == 409
    assert again.json()["detail"]["code"] == "request_not_pending"


async def test_approving_into_the_watchlist_and_managing_it(api):
    request_id = (await submit(api, ALICE)).json()["id"]
    approved = await api.client.post(
        f"/api/admin/security-requests/{request_id}/approval",
        json={"add_to_watchlist": True},
    )
    assert approved.json()["in_watchlist"] is True
    listed = await api.client.get("/api/admin/security-watchlist")
    codes = [e["security_code"] for e in listed.json()]
    assert len(codes) == 11 and "000001" in codes

    added = await api.client.post(
        "/api/admin/security-watchlist",
        json={"security_code": "000002", "note": "地产"},
    )
    assert added.status_code == 200 and len(added.json()) == 12
    bad = await api.client.post(
        "/api/admin/security-watchlist", json={"security_code": "abc"}
    )
    assert bad.status_code == 422
    removed = await api.client.post(
        "/api/admin/security-watchlist/000002/removal", json={"note": "不看了"}
    )
    assert removed.status_code == 200 and len(removed.json()) == 11
    gone = await api.client.post("/api/admin/security-watchlist/000002/removal")
    assert gone.status_code == 404
    everything = await api.client.get(
        "/api/admin/security-watchlist", params={"include_removed": "true"}
    )
    entry = next(e for e in everything.json() if e["security_code"] == "000002")
    assert (entry["removed_by"], entry["removal_note"]) == (OWNER, "不看了")


async def test_unknown_requests_are_not_found(api):
    for request_id in (str(uuid.uuid4()), "not-a-uuid"):
        for path in ("approval", "rejection"):
            response = await api.client.post(
                f"/api/admin/security-requests/{request_id}/{path}",
                json={"note": "x"} if path == "rejection" else None,
            )
            assert response.status_code == 404, (request_id, path)


# ---------------------------------------------------------------- 谁进得来
WEB_PATHS = [
    ("GET", "/api/web/v1/cross-app/origin"),
    ("GET", "/api/web/v1/securities/600519/availability"),
    ("POST", "/api/web/v1/security-requests"),
    ("GET", "/api/web/v1/security-requests"),
    ("GET", f"/api/web/v1/security-requests/{uuid.uuid4()}"),
    ("POST", f"/api/web/v1/security-requests/{uuid.uuid4()}/withdrawal"),
]
ADMIN_PATHS = [
    ("GET", "/api/admin/security-requests"),
    ("POST", f"/api/admin/security-requests/{uuid.uuid4()}/approval"),
    ("POST", f"/api/admin/security-requests/{uuid.uuid4()}/rejection"),
    ("GET", "/api/admin/security-watchlist"),
    ("POST", "/api/admin/security-watchlist"),
    ("POST", "/api/admin/security-watchlist/600519/removal"),
]


async def test_the_real_application_lets_nobody_in_without_the_right_session(
    monkeypatch,
):
    """AT-INFO-14 的后端一半、AT-INFO-15。用真的应用，只把身份服务换成假的。"""
    paths = {route.path for route in real_app.routes}
    for _, path in WEB_PATHS + ADMIN_PATHS:
        template = path.split("/")
        assert any(
            len(p.split("/")) == len(template)
            and all(
                a == b or a.startswith("{")
                for a, b in zip(p.split("/"), template, strict=True)
            )
            for p in paths
        ), path

    web = FakeAuthService({"member": browser_session("web", "profile:read")})
    admin = FakeAuthService(
        {
            "owner": browser_session("admin", "info:admin"),
            "clerk": browser_session("admin", "profile:read"),
        }
    )
    monkeypatch.setattr(auth_middleware, "web_auth_service", web)
    monkeypatch.setattr(auth_middleware, "admin_auth_service", admin)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=real_app), base_url="http://testserver"
    ) as client:
        for method, path in WEB_PATHS + ADMIN_PATHS:
            assert (await client.request(method, path)).status_code == 401, path
        # 用户的会话进不了管理面
        client.cookies.set("sunmoonai_info_web_sid", "member")
        for method, path in ADMIN_PATHS:
            assert (await client.request(method, path)).status_code == 401, path
        # 会改东西的请求还要来源与防伪令牌
        for method, path in WEB_PATHS:
            if method == "POST":
                assert (await client.request(method, path)).status_code == 403, path
        # 管理面：登录了但没有管理权限的进不来
        client.cookies.clear()
        client.cookies.set("sunmoonai_info_admin_sid", "clerk")
        for method, path in ADMIN_PATHS:
            if method == "GET":
                assert (await client.request(method, path)).status_code == 403, path
        # 管理员的会话进不了用户面
        client.cookies.set("sunmoonai_info_admin_sid", "owner")
        for method, path in WEB_PATHS:
            assert (await client.request(method, path)).status_code == 401, path
