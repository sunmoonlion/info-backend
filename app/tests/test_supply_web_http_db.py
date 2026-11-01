"""web端的提需求、我的需求、订阅、按主题找（SDD 0045 第一、二节；发版前补齐 F1）走 HTTP：
本人会话与本人令牌换成假的，需求、订阅走真数据库。"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi import FastAPI
from test_durable_delivery_db import db as db
from test_durable_delivery_db import sql
from test_supply_db import KEY
from test_supply_subscriptions_db import Subs

from app.application.errors.exceptions import ServiceUnavailableError
from app.application.supply.matching import PreMatch
from app.application.supply.mine import MyDemands
from app.domain.security import Principal
from app.infrastructure.repositories.supply_schedule import SqlScheduleRepository
from app.interfaces.errors.exception_handlers import register_exception_handlers
from app.interfaces.http.middleware import auth as middleware
from app.interfaces.http.web import supply as routes

PAGE = "https://www.stats.gov.cn/sj/zxfb/202610/t1.html"


def principal(subject: str) -> Principal:
    now = datetime.now(UTC)
    return Principal(
        actor_type="user",
        subject=subject,
        issuer="https://casdoor.test",
        app="knowfeed",
        surface="web",
        audience="knowfeed-web",
        authenticated_at=now,
        expires_at=now + timedelta(hours=1),
        policy_version="knowfeed-web-v2",
        actor_id=uuid.uuid5(uuid.NAMESPACE_URL, subject),
    )


def client_for(s: Subs, subject: str = "alice") -> httpx.AsyncClient:
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(routes.router, prefix="/api")
    app.dependency_overrides[middleware.get_web_current_user] = lambda: principal(
        subject
    )
    app.dependency_overrides[middleware.get_web_knowbase_token] = lambda: "user-token"
    app.dependency_overrides[routes.get_my_demands] = lambda: MyDemands(
        s.w.demands, s.w.repo, s.user, clock=s.w.clock
    )
    app.dependency_overrides[routes.get_demand_service] = lambda: s.w.demands
    app.dependency_overrides[routes.get_subscriptions] = lambda: s.service
    app.dependency_overrides[routes.get_searches] = lambda: s.searches
    app.dependency_overrides[routes.get_prematch] = lambda: PreMatch(
        s.w.repo,
        s.w.desk.repo,
        SqlScheduleRepository(s.w.repo._sessions),
        global_rate=60,
        clock=s.w.clock,
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://knowfeed"
    )


async def test_a_user_submits_sees_cancels_and_retries_own_demands(db):
    s = Subs(db)
    client = client_for(s)
    deadline = (s.w.clock.now + timedelta(days=3)).isoformat()
    body = {
        "kind": "description",
        "items": [{"key": KEY.as_dict()}],
        "readings": ["original", "pages"],
        "deadline": deadline,
        "note": "做估值用",
        "idempotency_key": "idem-web-0001",
    }
    made = await client.post("/api/web/v1/supply/demands", json=body)
    assert made.status_code == 201, made.text
    demand = made.json()["demand"]
    assert (demand["proposer"], demand["app"], demand["note"]) == (
        "user",
        "knowfeed",
        "做估值用",
    )
    # 本人提交即确认：凭本人令牌请 knowbase 记了确认，确认里只写一种读法（最费事的那种）
    confirm = next(c for c in s.user.calls if c[0] == "confirm")
    assert confirm[1] == "description" and confirm[2][0]["reading"] == "pages"
    # 点两次「提交」：回原来那张，不再记第二次确认
    again = await client.post("/api/web/v1/supply/demands", json=body)
    assert again.json()["replayed"] is True
    assert len([c for c in s.user.calls if c[0] == "confirm"]) == 1
    # 「最晚什么时候要」必填
    missing = await client.post(
        "/api/web/v1/supply/demands",
        json={**body, "idempotency_key": "idem-web-0002", "deadline": None},
    )
    assert missing.status_code == 422
    listed = await client.get("/api/web/v1/supply/demands")
    assert [d["demand_id"] for d in listed.json()] == [demand["demand_id"]]
    # 别人看不到
    other = client_for(s, "bob")
    seen = await other.get(f"/api/web/v1/supply/demands/{demand['demand_id']}")
    assert seen.status_code == 404
    cancelled = await client.post(
        f"/api/web/v1/supply/demands/{demand['demand_id']}/cancel"
    )
    item = cancelled.json()["items"][0]
    assert item["status"] == "cancelled"
    # 取消了的不能「再试一次」；无法满足的能，生成挂在原项下的新项
    refused = await client.post(
        f"/api/web/v1/supply/items/{item['item_id']}/retry",
        json={"deadline": deadline, "idempotency_key": "idem-web-retry1"},
    )
    assert (refused.status_code, refused.json()["code"]) == (409, "item_not_retryable")
    await sql(
        db,
        "UPDATE supply_demand_item SET status = 'unsatisfiable' WHERE id = :i",
        i=item["item_id"],
    )
    retried = await client.post(
        f"/api/web/v1/supply/items/{item['item_id']}/retry",
        json={"deadline": deadline, "idempotency_key": "idem-web-retry2"},
    )
    assert retried.status_code == 201, retried.text
    assert retried.json()["demand"]["items"][0]["retry_of"] == item["item_id"]


async def test_a_user_gives_a_url_and_sees_the_site_review_state(db):
    s = Subs(db)
    client = client_for(s)
    checked = await client.post(
        "/api/web/v1/supply/prematch",
        json={"items": [{"ref": "u1", "url": PAGE}]},
    )
    assert checked.json()["items"][0]["status"] == "site_pending"
    made = await client.post(
        "/api/web/v1/supply/demands",
        json={
            "kind": "url",
            "items": [
                {
                    "url": PAGE,
                    "follow_links": True,
                    "prefill": {"title": "公报"},
                    "declared_public": True,
                }
            ],
            "readings": ["original"],
            "deadline": (s.w.clock.now + timedelta(days=3)).isoformat(),
            "idempotency_key": "idem-web-url1",
        },
    )
    assert made.status_code == 201, made.text
    item = made.json()["demand"]["items"][0]
    assert (item["url"], item["status"], item["reason"], item["follow_links"]) == (
        PAGE,
        "accepted",
        "site_pending",
        True,
    )
    # 公开页面的声明记进需求项；没声明的拒
    assert item["declared_public"] is True


async def test_a_user_subscribes_and_manages_the_subscription(db):
    s = Subs(db)
    client = client_for(s)
    today = (s.w.clock.now + timedelta(hours=8)).date()
    made = await client.post(
        "/api/web/v1/supply/subscriptions",
        json={
            "by": "description",
            "subjects": ["000858.SZ"],
            "doc_types": ["interim_announcement"],
            "readings": ["original"],
            "ends_on": (today + timedelta(days=30)).isoformat(),
            "purpose": "跟踪公告",
            "monthly_cap": 20,
        },
    )
    assert made.status_code == 201, made.text
    sub = made.json()
    assert (sub["status"], sub["revision"], sub["renew_due"]) == ("active", 1, False)
    sid = sub["subscription_id"]
    paused = await client.post(f"/api/web/v1/supply/subscriptions/{sid}/pause")
    assert paused.json()["status"] == "paused"
    resumed = await client.post(f"/api/web/v1/supply/subscriptions/{sid}/resume")
    assert resumed.json()["status"] == "active"
    skip = await client.put(
        f"/api/web/v1/supply/subscriptions/{sid}/backlog", json={"skip": True}
    )
    assert skip.json()["skip_backlog"] is True
    renewed = await client.post(
        f"/api/web/v1/supply/subscriptions/{sid}/renew",
        json={"ends_on": (today + timedelta(days=300)).isoformat()},
    )
    assert renewed.json()["revision"] == 2
    detail = await client.get(f"/api/web/v1/supply/subscriptions/{sid}")
    assert detail.json()["rounds"] == []
    cancelled = await client.post(f"/api/web/v1/supply/subscriptions/{sid}/cancel")
    assert cancelled.json()["status"] == "cancelled"
    listed = await client.get("/api/web/v1/supply/subscriptions")
    assert [x["subscription_id"] for x in listed.json()] == [sid]
    options = await client.get("/api/web/v1/supply/options")
    assert "statistical_bulletin" in options.json()["org_doc_types"]


async def test_search_and_pick_over_http(db):
    from test_supply_subscriptions_db import _search_channel

    s = Subs(db)
    await _search_channel(s)
    client = client_for(s)
    started = await client.post(
        "/api/web/v1/supply/searches", json={"topic": "工业", "wanted": 5}
    )
    assert started.json()["status"] == "searching"
    await s.run_all()
    sid = started.json()["search_id"]
    found = await client.get(f"/api/web/v1/supply/searches/{sid}")
    assert [c["ref"] for c in found.json()["candidates"]] == ["k1", "k2"]
    picked = await client.post(
        f"/api/web/v1/supply/searches/{sid}/pick",
        json={
            "refs": ["k2"],
            "readings": ["original"],
            "deadline": (s.w.clock.now + timedelta(days=2)).isoformat(),
            "idempotency_key": "idem-web-pick1",
        },
    )
    assert picked.status_code == 201, picked.text
    assert picked.json()["items"][0]["url"] == "https://www.stats.gov.cn/r/1.html"


async def test_the_user_token_needs_the_knowbase_audience(monkeypatch):
    """没配 knowbase 的用户令牌受众（KNOWBASE_USER_AUDIENCE）：503，不去换令牌。"""
    from core.config import get_settings

    monkeypatch.setattr(get_settings(), "knowbase_user_audience", "")
    with pytest.raises(ServiceUnavailableError) as caught:
        await middleware.get_web_knowbase_token(None, "session")  # type: ignore[arg-type]
    assert caught.value.code == "knowbase_unconfigured"
