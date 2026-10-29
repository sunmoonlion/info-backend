"""管理端供给接口（SDD 0045 第 3.1 节、第四节）：要管理端会话；登记、试采、审核、启停、
待审网站、缺口清单、队列都走得通，拒绝照统一的错误形状回。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import httpx
from test_durable_delivery_db import db as db
from test_durable_delivery_db import sql
from test_supply_catalog_db import Desk
from test_supply_db import KEY, World

from app.domain.security import Principal
from app.interfaces.errors.exception_handlers import register_exception_handlers
from app.interfaces.http.admin import supply as routes


def operator() -> Principal:
    now = datetime.now(UTC)
    return Principal(
        actor_type="user",
        subject="casdoor/op-1",
        issuer="https://casdoor",
        app="knowfeed",
        surface="admin",
        audience="knowfeed-admin",
        actor_id=uuid4(),
        roles=("operator",),
        authenticated_at=now,
        expires_at=now + timedelta(hours=1),
        policy_version="v1",
    )


def client(desk: Desk | None = None) -> httpx.AsyncClient:
    from fastapi import FastAPI

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(routes.router, prefix="/api")
    if desk is not None:
        app.dependency_overrides[routes.require_knowfeed_admin] = operator
        app.dependency_overrides[routes.get_catalog] = lambda: desk.catalog
        app.dependency_overrides[routes.get_monitor] = lambda: desk.monitor
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://knowfeed"
    )


async def test_without_an_admin_session_nothing_is_served():
    answer = await client().get("/api/admin/v1/supply/channels")
    assert answer.status_code == 401


async def test_operators_register_trial_review_and_watch(db):
    w = World(db)
    desk = Desk(db, w)
    http = client(desk)
    listed = await http.get("/api/admin/v1/supply/channels")
    assert listed.status_code == 200
    assert [c["channel_id"] for c in listed.json()["items"]] == ["cninfo", "cninfo-irm"]
    assert {"name": "cninfo", "kind": "catalog"} in listed.json()["plugins"]

    body = {
        "channel_id": "sse",
        "name": "上交所定期报告",
        "kind": "catalog",
        "plugin": "cninfo",
        "doc_types": ["annual_report"],
        "readings": ["original", "pages"],
        "license": "via_expert",
        "rate_per_minute": 10,
        "daily_cap": 500,
        "domain": "sse.com.cn",
    }
    made = await http.post("/api/admin/v1/supply/channels", json=body)
    assert made.status_code == 201, made.text
    assert made.json()["review"] == "pending"
    again = await http.post("/api/admin/v1/supply/channels", json=body)
    assert (again.status_code, again.json()["code"]) == (409, "channel_exists")

    refused = await http.post(
        "/api/admin/v1/supply/channels/sse/review",
        json={"decision": "approve", "reason": "官方"},
    )
    assert (refused.status_code, refused.json()["code"]) == (409, "trial_required")
    w.clock.now += timedelta(seconds=1)
    trial = await http.post(
        "/api/admin/v1/supply/channels/sse/trials",
        json={
            "subject": KEY.subject,
            "doc_type": KEY.doc_type,
            "period_start": "2025-01-01",
            "period_end": "2025-12-31",
        },
    )
    assert trial.status_code == 202 and trial.json()["status"] == "queued"
    await desk.trials.run(trial.json()["trial_id"])
    approved = await http.post(
        "/api/admin/v1/supply/channels/sse/review",
        json={"decision": "approve", "reason": "交易所官方披露"},
    )
    assert approved.json()["review"] == "approved"
    detail = await http.get("/api/admin/v1/supply/channels/sse")
    assert detail.json()["trials"][0]["status"] == "ok"
    assert detail.json()["events"][0] == {
        **detail.json()["events"][0],
        "action": "approve",
        "actor": "casdoor/op-1",
    }

    paused = await http.post(
        "/api/admin/v1/supply/channels/sse/status",
        json={"action": "pause", "reason": "源站改版"},
    )
    assert paused.json()["status"] == "paused"
    order = await http.post(
        "/api/admin/v1/supply/channels/order", json={"channel_ids": ["sse"]}
    )
    assert [c["channel_id"] for c in order.json()["items"]] == [
        "sse",
        "cninfo",
        "cninfo-irm",
    ]

    w.confirm("conf-1", "alice", KEY)
    await w.submit()
    queue = await http.get("/api/admin/v1/supply/queue")
    assert queue.status_code == 200
    assert {c["channel_id"]: c["queued"] for c in queue.json()["channels"]} == {
        "sse": 1,
        "cninfo": 0,
        "cninfo-irm": 0,
    }
    job_id = str(await sql(db, "SELECT id FROM supply_job"))
    jobs = await http.get("/api/admin/v1/supply/jobs", params={"status": "queued"})
    assert [j["job_id"] for j in jobs.json()["items"]] == [job_id]
    requeue = await http.post(f"/api/admin/v1/supply/jobs/{job_id}/requeue")
    assert (requeue.status_code, requeue.json()["code"]) == (409, "channel_not_active")

    await desk.catalog.ensure_site("https://www.stats.gov.cn/a.html")
    sites = await http.get("/api/admin/v1/supply/sites")
    assert sites.json()["pending"] == 1
    assert sites.json()["items"][0]["domain"] == "www.stats.gov.cn"
    gaps = await http.get("/api/admin/v1/supply/gaps", params={"kind": "uncovered"})
    assert gaps.status_code == 200 and gaps.json()["items"] == []
    bad = await http.get("/api/admin/v1/supply/gaps", params={"kind": "nope"})
    assert bad.status_code == 422
