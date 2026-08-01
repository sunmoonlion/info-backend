from __future__ import annotations

import time
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey

from app.application.errors.exceptions import (
    ServiceUnavailableError,
    UnauthorizedError,
)
from app.infrastructure.security.oidc import OidcProviderClient
from core.config import Settings


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        env="production",
        casdoor_endpoint="https://identity.example.test",
        casdoor_client_id="tpl-admin-client",
        casdoor_client_secret="test-only-secret",
        casdoor_redirect_uri="https://admin.example.test/api/auth/callback",
        frontend_base_url="https://admin.example.test",
        frontend_allowed_origins="https://admin.example.test",
        allowed_hosts="admin.example.test",
    )


def _token(key: RSAKey, **overrides: object) -> str:
    now = int(time.time())
    claims: dict[str, object] = {
        "iss": "https://identity.example.test",
        "sub": "user-123",
        "aud": "tpl-admin-client",
        "iat": now,
        "exp": now + 300,
        "nonce": "nonce-123",
        "name": "Test User",
    }
    claims.update(overrides)
    return jwt.encode(
        {"alg": "RS256", "kid": "test-key"},
        claims,
        key,
        algorithms=["RS256"],
    )


def _client(encoded: str, key: RSAKey) -> OidcProviderClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": "https://identity.example.test",
                    "authorization_endpoint": (
                        "https://identity.example.test/login/oauth/authorize"
                    ),
                    "token_endpoint": (
                        "https://identity.example.test/api/login/oauth/access_token"
                    ),
                    "jwks_uri": "https://identity.example.test/.well-known/jwks",
                },
            )
        if request.url.path.endswith("/jwks"):
            return httpx.Response(200, json={"keys": [key.as_dict(private=False)]})
        if request.url.path.endswith("/access_token"):
            form = parse_qs(request.content.decode())
            assert form["code_verifier"] == ["verifier-123"]
            return httpx.Response(
                200,
                json={
                    "id_token": encoded,
                    "access_token": "must-not-persist",
                },
            )
        raise AssertionError(f"unexpected request: {request.url}")

    return OidcProviderClient(_settings(), transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_authorization_requests_the_canonical_admin_scope() -> None:
    key = RSAKey.generate_key(parameters={"kid": "test-key"})
    authorization_url = await _client(_token(key), key).build_authorization_url(
        state="state-123",
        nonce="nonce-123",
        code_challenge="challenge-123",
    )

    query = parse_qs(urlsplit(authorization_url).query)
    assert query["scope"] == ["openid profile email info:admin"]


@pytest.mark.asyncio
async def test_code_exchange_verifies_signature_claims_and_pkce() -> None:
    key = RSAKey.generate_key(parameters={"kid": "test-key"})
    claims = await _client(_token(key), key).exchange_authorization_code(
        code="code-123",
        code_verifier="verifier-123",
        nonce="nonce-123",
    )
    assert claims["sub"] == "user-123"
    assert "access_token" not in claims


@pytest.mark.asyncio
async def test_backchannel_preserves_public_issuer_and_host() -> None:
    key = RSAKey.generate_key(parameters={"kid": "test-key"})
    encoded = _token(key)
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "casdoor-sunmoonai"
        assert request.headers["host"] == "identity.example.test"
        paths.append(request.url.path)
        if request.url.path.endswith("/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": "https://identity.example.test",
                    "authorization_endpoint": (
                        "https://identity.example.test/login/oauth/authorize"
                    ),
                    "token_endpoint": (
                        "https://identity.example.test/api/login/oauth/access_token"
                    ),
                    "jwks_uri": "https://identity.example.test/.well-known/jwks",
                },
            )
        if request.url.path.endswith("/jwks"):
            return httpx.Response(200, json={"keys": [key.as_dict(private=False)]})
        if request.url.path.endswith("/access_token"):
            return httpx.Response(200, json={"id_token": encoded})
        raise AssertionError(f"unexpected request: {request.url}")

    settings = _settings().model_copy(
        update={"casdoor_backchannel_endpoint": "http://casdoor-sunmoonai:8000"}
    )
    client = OidcProviderClient(settings, transport=httpx.MockTransport(handler))
    await client.build_authorization_url(
        state="state",
        nonce="nonce-123",
        code_challenge="challenge",
    )
    claims = await client.exchange_authorization_code(
        code="code",
        code_verifier="verifier-123",
        nonce="nonce-123",
    )

    assert claims["iss"] == "https://identity.example.test"
    assert paths == [
        "/.well-known/openid-configuration",
        "/api/login/oauth/access_token",
        "/.well-known/jwks",
    ]


@pytest.mark.asyncio
async def test_client_credentials_uses_validated_transport() -> None:
    requests: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "casdoor-sunmoonai"
        assert request.headers["host"] == "identity.example.test"
        requests.append((request.method, request.url.path))
        if request.url.path.endswith("/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": "https://identity.example.test",
                    "authorization_endpoint": (
                        "https://identity.example.test/login/oauth/authorize"
                    ),
                    "token_endpoint": (
                        "https://identity.example.test/api/login/oauth/access_token"
                    ),
                    "jwks_uri": "https://identity.example.test/.well-known/jwks",
                },
            )
        if request.url.path.endswith("/access_token"):
            form = parse_qs(request.content.decode())
            assert form["scope"] == ["downstream:read"]
            return httpx.Response(
                200,
                json={"access_token": "opaque-service-token", "expires_in": 300},
            )
        raise AssertionError(f"unexpected request: {request.url}")

    settings = _settings().model_copy(
        update={"casdoor_backchannel_endpoint": "http://casdoor-sunmoonai:8000"}
    )
    client = OidcProviderClient(settings, transport=httpx.MockTransport(handler))
    result = await client.exchange_client_credentials(scope="downstream:read")

    assert result["expires_in"] == 300
    assert requests == [
        ("GET", "/.well-known/openid-configuration"),
        ("POST", "/api/login/oauth/access_token"),
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "other-client"},
        {"aud": ["tpl-admin-client", "other-client"]},
        {"nonce": "wrong-nonce"},
        {"exp": int(time.time()) - 60},
        {"iat": int(time.time()) + 300},
        {"nbf": int(time.time()) + 300},
        {"iss": "https://attacker.example.test"},
    ],
)
async def test_id_token_claim_failures_are_rejected(
    overrides: dict[str, object],
) -> None:
    key = RSAKey.generate_key(parameters={"kid": "test-key"})
    with pytest.raises(UnauthorizedError):
        await _client(_token(key, **overrides), key).exchange_authorization_code(
            code="code-123",
            code_verifier="verifier-123",
            nonce="nonce-123",
        )


@pytest.mark.asyncio
async def test_unknown_signing_key_is_rejected_after_refresh() -> None:
    trusted = RSAKey.generate_key(parameters={"kid": "test-key"})
    attacker = RSAKey.generate_key(parameters={"kid": "attacker-key"})
    with pytest.raises(UnauthorizedError):
        await _client(_token(attacker), trusted).exchange_authorization_code(
            code="code-123",
            code_verifier="verifier-123",
            nonce="nonce-123",
        )


@pytest.mark.asyncio
async def test_discovery_cannot_redirect_jwks_cross_origin() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "issuer": "https://identity.example.test",
                "authorization_endpoint": (
                    "https://identity.example.test/login/oauth/authorize"
                ),
                "token_endpoint": (
                    "https://identity.example.test/api/login/oauth/access_token"
                ),
                "jwks_uri": "https://attacker.example.test/jwks",
            },
        )

    client = OidcProviderClient(_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(ServiceUnavailableError, match="cross-origin"):
        await client.get_metadata()


@pytest.mark.asyncio
async def test_custom_discovery_cannot_send_credentials_cross_origin() -> None:
    settings = _settings().model_copy(
        update={
            "casdoor_discovery_url": (
                "https://attacker.example.test/.well-known/config"
            )
        }
    )
    client = OidcProviderClient(
        settings,
        transport=httpx.MockTransport(lambda _: httpx.Response(200)),
    )
    with pytest.raises(ServiceUnavailableError, match="cross-origin"):
        await client.get_metadata()
