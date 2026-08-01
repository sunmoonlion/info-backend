from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from fastapi.routing import APIRoute

import app.interfaces.endpoints.auth_routes as auth_routes
import app.interfaces.middleware.auth as auth_middleware
from app.domain.security import BrowserSession, Principal
from app.main import app, settings


class FakeAuthService:
    def __init__(self, sessions: dict[str, BrowserSession]) -> None:
        self.sessions = sessions
        self.deleted: list[str | None] = []

    async def get_browser_session(
        self, session_id: str | None
    ) -> BrowserSession | None:
        return self.sessions.get(session_id or "")

    def validate_csrf(
        self,
        *,
        session: BrowserSession,
        method: str,
        origin: str | None,
        csrf_token: str | None,
    ) -> None:
        if method not in {"GET", "HEAD", "OPTIONS"} and (
            origin != settings.frontend_origin_list[0]
            or csrf_token != session.csrf_token
        ):
            from app.application.errors.exceptions import ForbiddenError

            raise ForbiddenError("CSRF validation failed")

    @staticmethod
    def require_scopes(
        principal: Principal, required: set[str] | frozenset[str]
    ) -> None:
        if not principal.has_scopes(required):
            from app.application.errors.exceptions import ForbiddenError

            raise ForbiddenError("Required scope missing")

    async def delete_session(self, session_id: str | None) -> None:
        self.deleted.append(session_id)


def _session(*scopes: str) -> BrowserSession:
    now = datetime.now(UTC)
    return BrowserSession(
        principal=Principal(
            actor_type="user",
            subject="user-123",
            issuer="https://identity.example.test",
            app="info",
            surface="admin",
            audience="tpl-admin-client",
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
async def test_auth_routes_fail_closed_and_me_is_minimal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeAuthService({"no-scope": _session(), "admin": _session("info:admin")})
    monkeypatch.setattr(auth_middleware, "_auth_service", fake)
    monkeypatch.setattr(auth_routes, "_auth_service", fake)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        anonymous = await client.get("/api/auth/me")
        assert anonymous.status_code == 401

        client.cookies.set("sunmoonai_info_admin_sid", "admin")
        response = await client.get(
            "/api/auth/me",
            headers={
                "X-Correlation-ID": "corr-auth-001",
                "X-Operation-ID": "op-auth-001",
            },
        )
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-correlation-id"] == "corr-auth-001"
        assert response.headers["x-operation-id"] == "op-auth-001"
        rendered = str(response.json())
        assert "subject" not in rendered
        assert "audience" not in rendered
        assert "access_token" not in rendered
        assert "id_token" not in rendered

        client.cookies.set("sunmoonai_info_admin_sid", "no-scope")
        protected = await client.post(
            "/api/internal/tasks/ping",
            headers={
                "Origin": settings.frontend_origin_list[0],
                "X-CSRF-Token": ("csrf-token-with-at-least-thirty-two-characters"),
            },
        )
        assert protected.status_code == 403


@pytest.mark.asyncio
async def test_logout_is_post_only_and_csrf_protected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeAuthService({"admin": _session("info:admin")})
    monkeypatch.setattr(auth_middleware, "_auth_service", fake)
    monkeypatch.setattr(auth_routes, "_auth_service", fake)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        client.cookies.set("sunmoonai_info_admin_sid", "admin")
        assert (await client.get("/api/auth/logout")).status_code == 405
        assert (await client.post("/api/auth/logout")).status_code == 403
        allowed = await client.post(
            "/api/auth/logout",
            headers={
                "Origin": settings.frontend_origin_list[0],
                "X-CSRF-Token": ("csrf-token-with-at-least-thirty-two-characters"),
            },
        )
        assert allowed.status_code == 204
        assert allowed.headers["cache-control"] == "no-store"
        assert fake.deleted == ["admin"]


@pytest.mark.asyncio
async def test_health_and_security_headers_are_public() -> None:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://testserver"
    ) as client:
        response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["referrer-policy"] == "no-referrer"


def test_every_non_auth_api_route_requires_a_scope_dependency() -> None:
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        if not route.path.startswith("/api/") or route.path.startswith("/api/auth/"):
            continue
        calls = {
            getattr(dependency.call, "__name__", "")
            for dependency in route.dependant.dependencies
        }
        assert "dependency" in calls, route.path
