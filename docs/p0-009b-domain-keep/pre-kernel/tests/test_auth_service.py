from __future__ import annotations

import json
import time
import uuid
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

import app.application.services.auth_service as auth_module
from app.application.errors.exceptions import ForbiddenError, UnauthorizedError
from app.application.services.auth_service import (
    SESSION_PREFIX,
    TRANSACTION_PREFIX,
    AuthService,
)
from core.config import Settings


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def set(self, key: str, value: str, *, ex: int, nx: bool = False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def get(self, key: str):
        return self.values.get(key)

    async def getdel(self, key: str):
        return self.values.pop(key, None)

    async def delete(self, key: str):
        return int(self.values.pop(key, None) is not None)


class FakeRedisHolder:
    def __init__(self, client: FakeRedis) -> None:
        self.client = client


class FakeOidc:
    def __init__(self) -> None:
        self.code_verifier: str | None = None

    async def build_authorization_url(self, *, state: str, nonce: str, code_challenge: str) -> str:
        return (
            "https://identity.example.test/authorize?"
            f"state={state}&nonce={nonce}&code_challenge={code_challenge}"
        )

    async def exchange_authorization_code(self, *, code: str, code_verifier: str, nonce: str):
        self.code_verifier = code_verifier
        now = int(time.time())
        return {
            "iss": "https://identity.example.test/.well-known/sunmoonai-info-admin",
            "sub": "user-123",
            "aud": "info-admin-client",
            "iat": now,
            "exp": now + 600,
            "nonce": nonce,
            "name": "Test User",
            "access_token": "must-not-persist",
        }


class StubAuthService(AuthService):
    async def _load_or_create_user(
        self, issuer: str, subject: str, claims: dict[str, Any]
    ) -> dict[str, Any]:
        return {
            "id": uuid.UUID("00000000-0000-4000-8000-000000000001"),
            "display_name": "Test User",
            "email": "user@example.test",
            "roles": ["editor"],
            "scopes": ["info:admin"],
        }


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        casdoor_endpoint="https://identity.example.test",
        casdoor_client_id="info-admin-client",
        casdoor_client_secret="test-only-secret",
        casdoor_redirect_uri="https://info.example.test/api/auth/callback",
        frontend_base_url="https://info.example.test",
        casdoor_verify_ssl=True,
        env="production",
    )


def test_auth_keys_stay_inside_info_redis_acl_namespace() -> None:
    assert SESSION_PREFIX.startswith("info:")
    assert TRANSACTION_PREFIX.startswith("info:")


@pytest.mark.asyncio
async def test_login_transaction_is_one_time_and_session_contains_no_provider_token(monkeypatch) -> None:
    redis = FakeRedis()
    monkeypatch.setattr(auth_module, "get_redis", lambda: FakeRedisHolder(redis))
    oidc = FakeOidc()
    service = StubAuthService(_settings(), oidc_client=oidc)  # type: ignore[arg-type]

    start = await service.begin_login("//attacker.example.test/path")
    query = parse_qs(urlsplit(start.authorization_url).query)
    state = query["state"][0]
    session_start, return_to = await service.complete_login(
        code="code-123",
        state=state,
        transaction_id=start.transaction_id,
    )

    assert return_to == "/"
    assert oidc.code_verifier is not None
    raw_session = redis.values[f"{SESSION_PREFIX}{session_start.session_id}"]
    assert "access_token" not in raw_session
    assert "id_token" not in raw_session
    parsed = json.loads(raw_session)
    assert parsed["principal"]["scopes"] == ["info:admin"]

    with pytest.raises(UnauthorizedError, match="transaction invalid"):
        await service.complete_login(
            code="code-123",
            state=state,
            transaction_id=start.transaction_id,
        )


@pytest.mark.asyncio
async def test_state_mismatch_consumes_transaction(monkeypatch) -> None:
    redis = FakeRedis()
    monkeypatch.setattr(auth_module, "get_redis", lambda: FakeRedisHolder(redis))
    service = StubAuthService(_settings(), oidc_client=FakeOidc())  # type: ignore[arg-type]
    start = await service.begin_login("/safe")
    with pytest.raises(UnauthorizedError, match="state mismatch"):
        await service.complete_login(
            code="code-123",
            state="attacker-state",
            transaction_id=start.transaction_id,
        )
    with pytest.raises(UnauthorizedError, match="transaction invalid"):
        await service.complete_login(
            code="code-123",
            state="attacker-state",
            transaction_id=start.transaction_id,
        )


@pytest.mark.asyncio
async def test_csrf_requires_allowed_origin_and_constant_session_token(monkeypatch) -> None:
    redis = FakeRedis()
    monkeypatch.setattr(auth_module, "get_redis", lambda: FakeRedisHolder(redis))
    service = StubAuthService(_settings(), oidc_client=FakeOidc())  # type: ignore[arg-type]
    start = await service.begin_login("/")
    state = parse_qs(urlsplit(start.authorization_url).query)["state"][0]
    session_start, _ = await service.complete_login(
        code="code-123", state=state, transaction_id=start.transaction_id
    )
    session = await service.get_browser_session(session_start.session_id)
    assert session is not None

    service.validate_csrf(session=session, method="GET", origin=None, csrf_token=None)
    with pytest.raises(ForbiddenError, match="origin"):
        service.validate_csrf(
            session=session,
            method="POST",
            origin="https://attacker.example.test",
            csrf_token=session.csrf_token,
        )
    with pytest.raises(ForbiddenError, match="CSRF"):
        service.validate_csrf(
            session=session,
            method="POST",
            origin="https://info.example.test",
            csrf_token="wrong-token",
        )
    service.validate_csrf(
        session=session,
        method="POST",
        origin="https://info.example.test",
        csrf_token=session.csrf_token,
    )
