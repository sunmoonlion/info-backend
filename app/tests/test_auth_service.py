from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest

import app.application.services.auth_service as auth_module
from app.application.errors.exceptions import ForbiddenError, UnauthorizedError
from app.application.services.auth_service import AuthService
from app.domain.security import BrowserSession, Principal
from core.config import Settings


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, key: str, value: str, **kwargs: object) -> bool:
        if kwargs.get("nx") and key in self.values:
            return False
        self.values[key] = value
        return True

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def getdel(self, key: str) -> str | None:
        return self.values.pop(key, None)

    async def delete(self, key: str) -> int:
        return int(self.values.pop(key, None) is not None)


class FakeRedisHolder:
    def __init__(self, client: FakeRedis) -> None:
        self.client = client


class FakeOidc:
    async def build_authorization_url(self, **values: str) -> str:
        return f"https://identity.example.test/authorize?state={values['state']}"

    async def exchange_authorization_code(self, **_: str) -> dict[str, object]:
        now = int(datetime.now(UTC).timestamp())
        return {
            "iss": "https://identity.example.test",
            "sub": "user-123",
            "iat": now,
            "exp": now + 600,
            "name": "Test User",
            "roles": ["editor", "provider-admin"],
            "scope": "profile:read root:all info:admin",
        }


class StubAuthService(AuthService):
    async def _load_or_create_user(
        self, issuer: str, subject: str, claims: dict[str, object]
    ) -> dict[str, object]:
        del issuer, subject, claims
        return {
            "id": uuid.UUID("00000000-0000-4000-8000-000000000001"),
            "display_name": "Test User",
            "email": "user@example.test",
            "roles": ["editor"],
            "scopes": ["profile:read", "info:admin"],
        }


def settings() -> Settings:
    return Settings(
        _env_file=None,
        admin_casdoor_client_id="tpl-admin-client",
        admin_casdoor_client_secret="admin-secret",
        admin_casdoor_redirect_uri="https://admin.example.test/api/auth/admin/callback",
        admin_frontend_base_url="https://admin.example.test",
        admin_auth_role_allowlist="editor,member",
        admin_auth_scope_allowlist="info:admin,profile:read",
        web_casdoor_client_id="tpl-web-client",
        web_casdoor_client_secret="web-secret",
        web_casdoor_redirect_uri="https://web.example.test/api/auth/web/callback",
        web_frontend_base_url="https://web.example.test",
        web_auth_role_allowlist="editor,member",
        web_auth_scope_allowlist="profile:read",
    )


@pytest.mark.asyncio
async def test_surface_transaction_and_session_namespaces_cannot_cross(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    monkeypatch.setattr(auth_module, "get_redis", lambda: FakeRedisHolder(redis))
    config = settings()
    admin = StubAuthService("admin", config, FakeOidc())  # type: ignore[arg-type]
    web = StubAuthService("web", config, FakeOidc())  # type: ignore[arg-type]

    start = await admin.begin_login("/settings")
    state = parse_qs(urlsplit(start.authorization_url).query)["state"][0]
    with pytest.raises(UnauthorizedError, match="transaction is invalid"):
        await web.complete_login(
            code="code", state=state, transaction_id=start.transaction_id
        )

    session_start, return_to = await admin.complete_login(
        code="code", state=state, transaction_id=start.transaction_id
    )
    assert return_to == "/settings"
    session = await admin.get_browser_session(session_start.session_id)
    assert session is not None and session.principal.surface == "admin"
    assert await web.get_browser_session(session_start.session_id) is None
    raw = redis.values[
        f"{config.browser_profile('admin').session_key_prefix}"
        f"{session_start.session_id}"
    ]
    assert "access_token" not in raw and "id_token" not in raw


@pytest.mark.asyncio
async def test_admin_signup_is_forbidden_but_web_signup_is_supported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    monkeypatch.setattr(auth_module, "get_redis", lambda: FakeRedisHolder(redis))
    config = settings()
    with pytest.raises(UnauthorizedError):
        await StubAuthService(
            "admin", config, FakeOidc()  # type: ignore[arg-type]
        ).begin_login(mode="signup")
    result = await StubAuthService(
        "web", config, FakeOidc()  # type: ignore[arg-type]
    ).begin_login(mode="signup")
    assert result.transaction_id


@pytest.mark.asyncio
async def test_csrf_is_bound_to_each_surface_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    monkeypatch.setattr(auth_module, "get_redis", lambda: FakeRedisHolder(redis))
    service = StubAuthService(
        "web", settings(), FakeOidc()  # type: ignore[arg-type]
    )
    start = await service.begin_login()
    state = parse_qs(urlsplit(start.authorization_url).query)["state"][0]
    created, _ = await service.complete_login(
        code="code", state=state, transaction_id=start.transaction_id
    )
    session = await service.get_browser_session(created.session_id)
    assert session is not None
    with pytest.raises(ForbiddenError, match="origin"):
        service.validate_csrf(
            session=session,
            method="POST",
            origin="https://admin.example.test",
            csrf_token=session.csrf_token,
        )
    service.validate_csrf(
        session=session,
        method="POST",
        origin="https://web.example.test",
        csrf_token=session.csrf_token,
    )


@pytest.mark.asyncio
async def test_policy_or_surface_change_invalidates_existing_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    redis = FakeRedis()
    monkeypatch.setattr(auth_module, "get_redis", lambda: FakeRedisHolder(redis))
    config = settings()
    profile = config.browser_profile("web")
    now = datetime.now(UTC)
    session = BrowserSession(
        principal=Principal(
            actor_type="user",
            subject="user-123",
            issuer="https://identity.example.test",
            app="info",
            surface="admin",
            audience=profile.client_id,
            actor_id=uuid.UUID("00000000-0000-4000-8000-000000000001"),
            authenticated_at=now,
            expires_at=now + timedelta(minutes=5),
            policy_version=profile.policy_version,
        ),
        csrf_token="csrf-token-with-at-least-thirty-two-characters",
    )
    redis.values[f"{profile.session_key_prefix}wrong"] = session.model_dump_json()
    service = AuthService("web", config, FakeOidc())  # type: ignore[arg-type]
    assert await service.get_browser_session("wrong") is None
    assert not redis.values


def test_provider_claims_are_filtered_by_surface_local_allowlist() -> None:
    service = AuthService(
        "web", settings(), FakeOidc()  # type: ignore[arg-type]
    )
    assert service._allowed_claims(
        (["editor", "provider-admin"],), service.profile.role_allowlist
    ) == ["editor"]
    assert service._allowed_claims(
        ("profile:read root:all",), service.profile.scope_allowlist
    ) == ["profile:read"]
