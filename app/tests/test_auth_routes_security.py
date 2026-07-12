from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.routing import APIRoute

import app.interfaces.endpoints.auth_routes as auth_routes
import app.interfaces.middleware.auth as auth_middleware
from app.application.services import info_crawl_service
from app.domain.security import BrowserSession, Principal
from app.infrastructure.storage.postgres import get_db_session
from app.interfaces.endpoints.info_routes import review_document
from app.interfaces.schemas.info import DocumentReviewRequest
from app.main import app


class FakeAuthService:
    def __init__(self, sessions: dict[str, BrowserSession]) -> None:
        self.sessions = sessions
        self.deleted: list[str | None] = []

    async def get_browser_session(self, session_id: str | None):
        return self.sessions.get(session_id or "")

    def validate_csrf(self, *, session, method, origin, csrf_token):
        if method not in {"GET", "HEAD", "OPTIONS"}:
            if origin != "http://localhost:5173" or csrf_token != session.csrf_token:
                from app.application.errors.exceptions import ForbiddenError

                raise ForbiddenError("CSRF validation failed")

    @staticmethod
    def require_scopes(principal: Principal, required: set[str] | frozenset[str]):
        if not principal.has_scopes(required):
            from app.application.errors.exceptions import ForbiddenError

            raise ForbiddenError("Required scope missing")

    async def delete_session(self, session_id: str | None):
        self.deleted.append(session_id)


def _session(*scopes: str) -> BrowserSession:
    now = datetime.now(UTC)
    return BrowserSession(
        principal=Principal(
            actor_type="user",
            subject="user-123",
            issuer="https://identity.example.test/.well-known/sunmoonai-info-admin",
            app="info",
            surface="admin",
            audience="info-admin-client",
            actor_id=uuid.UUID("00000000-0000-4000-8000-000000000001"),
            display_name="Test User",
            email="user@example.test",
            roles=("editor",),
            scopes=frozenset(scopes),
            authenticated_at=now,
            expires_at=now + timedelta(minutes=10),
            policy_version="info-admin-v1",
        ),
        csrf_token="csrf-token-with-at-least-thirty-two-characters",
    )


@pytest.mark.asyncio
async def test_admin_routes_fail_closed_and_me_does_not_leak_provider_claims(monkeypatch) -> None:
    fake = FakeAuthService({"no-scope": _session(), "admin": _session("info:admin")})
    monkeypatch.setattr(auth_middleware, "_auth_service", fake)
    monkeypatch.setattr(auth_routes, "_auth_service", fake)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        anonymous = await client.get("/api/admin/sources")
        assert anonymous.status_code == 401

        client.cookies.set("sunmoonai_info_admin_sid", "no-scope")
        denied = await client.get("/api/admin/sources")
        assert denied.status_code == 403

        client.cookies.set("sunmoonai_info_admin_sid", "admin")
        me = await client.get("/api/auth/me")
        assert me.status_code == 200
        payload = me.json()
        assert payload["authenticated"] is True
        rendered = str(payload)
        assert "subject" not in rendered
        assert "audience" not in rendered
        assert "access_token" not in rendered
        assert "id_token" not in rendered

        internal = await client.post("/api/internal/tasks/ping")
        assert internal.status_code == 404


@pytest.mark.asyncio
async def test_logout_is_post_only_and_csrf_protected(monkeypatch) -> None:
    fake = FakeAuthService({"admin": _session("info:admin")})
    monkeypatch.setattr(auth_middleware, "_auth_service", fake)
    monkeypatch.setattr(auth_routes, "_auth_service", fake)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client.cookies.set("sunmoonai_info_admin_sid", "admin")
        assert (await client.get("/api/auth/logout")).status_code == 405
        denied = await client.post("/api/auth/logout")
        assert denied.status_code == 403
        allowed = await client.post(
            "/api/auth/logout",
            headers={
                "Origin": "http://localhost:5173",
                "X-CSRF-Token": "csrf-token-with-at-least-thirty-two-characters",
            },
        )
        assert allowed.status_code == 204
        assert fake.deleted == ["admin"]


@pytest.mark.asyncio
async def test_authorized_admin_read_is_allowed(monkeypatch) -> None:
    fake = FakeAuthService({"admin": _session("info:admin")})
    monkeypatch.setattr(auth_middleware, "_auth_service", fake)
    monkeypatch.setattr(auth_routes, "_auth_service", fake)

    async def fake_session():
        yield object()

    async def fake_list_sources(_):
        return []

    app.dependency_overrides[get_db_session] = fake_session
    monkeypatch.setattr(info_crawl_service, "list_sources", fake_list_sources)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            client.cookies.set("sunmoonai_info_admin_sid", "admin")
            response = await client.get("/api/admin/sources")
            assert response.status_code == 200
            assert response.json() == []
    finally:
        app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_reviewer_is_derived_from_principal_not_payload(monkeypatch) -> None:
    captured: dict = {}

    async def fake_review(_, **kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(info_crawl_service, "review_document", fake_review)
    principal = _session("info:admin").principal
    await review_document(
        document_id=uuid.uuid4(),
        payload=DocumentReviewRequest(
            status="approved",
            reviewer="attacker-controlled-reviewer",
            reason="test",
        ),
        principal=principal,
        session=object(),  # type: ignore[arg-type]
    )
    assert captured["reviewer"] == str(principal.actor_id)
    assert captured["reviewer"] != "attacker-controlled-reviewer"


def test_every_non_auth_api_route_has_admin_auth_dependency() -> None:
    for route in app.routes:
        if not isinstance(route, APIRoute) or not route.path.startswith("/api/"):
            continue
        if route.path.startswith("/api/auth/"):
            continue
        calls = {getattr(dependency.call, "__name__", "") for dependency in route.dependant.dependencies}
        assert "dependency" in calls, route.path
