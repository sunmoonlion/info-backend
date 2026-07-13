from __future__ import annotations

import time
from urllib.parse import parse_qs

import httpx
import pytest
from joserfc import jwt
from joserfc.jwk import RSAKey

from app.application.errors.exceptions import ServiceUnavailableError, UnauthorizedError
from app.infrastructure.security.oidc import OidcProviderClient
from core.config import Settings


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        casdoor_endpoint="https://identity.example.test",
        casdoor_client_id="info-admin-client",
        casdoor_client_secret="test-only-secret",
        casdoor_redirect_uri="https://info.example.test/api/auth/callback",
        casdoor_application="sunmoonai-info-admin",
        frontend_base_url="https://info.example.test",
        casdoor_verify_ssl=True,
        env="production",
    )


def _token(key: RSAKey, **overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://identity.example.test",
        "sub": "user-123",
        "aud": "info-admin-client",
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


def _client(token: str, key: RSAKey) -> OidcProviderClient:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/openid-configuration"):
            return httpx.Response(
                200,
                json={
                    "issuer": "https://identity.example.test",
                    "authorization_endpoint": "https://identity.example.test/login/oauth/authorize",
                    "token_endpoint": "https://identity.example.test/api/login/oauth/access_token",
                    "jwks_uri": "https://identity.example.test/.well-known/jwks",
                },
            )
        if request.url.path.endswith("/jwks"):
            return httpx.Response(200, json={"keys": [key.as_dict(private=False)]})
        if request.url.path.endswith("/access_token"):
            form = parse_qs(request.content.decode())
            assert form["code_verifier"] == ["verifier-123"]
            return httpx.Response(200, json={"id_token": token, "access_token": "must-not-persist"})
        raise AssertionError(f"unexpected request: {request.url}")

    return OidcProviderClient(_settings(), transport=httpx.MockTransport(handler))


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
async def test_backchannel_transport_preserves_public_issuer_and_host() -> None:
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
                    "authorization_endpoint": "https://identity.example.test/login/oauth/authorize",
                    "token_endpoint": "https://identity.example.test/api/login/oauth/access_token",
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
    authorization_url = await client.build_authorization_url(
        state="state", nonce="nonce-123", code_challenge="challenge"
    )
    claims = await client.exchange_authorization_code(
        code="code", code_verifier="verifier", nonce="nonce-123"
    )

    assert authorization_url.startswith("https://identity.example.test/login/oauth/authorize?")
    assert claims["iss"] == "https://identity.example.test"
    assert paths == [
        "/.well-known/openid-configuration",
        "/api/login/oauth/access_token",
        "/.well-known/jwks",
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"aud": "research-admin-client"},
        {"aud": ["info-admin-client", "research-admin-client"]},
        {"nonce": "wrong-nonce"},
        {"exp": int(time.time()) - 60},
        {"iss": "https://attacker.example.test"},
    ],
)
async def test_id_token_claim_failures_are_rejected(overrides: dict) -> None:
    key = RSAKey.generate_key(parameters={"kid": "test-key"})
    client = _client(_token(key, **overrides), key)
    with pytest.raises(UnauthorizedError):
        await client.exchange_authorization_code(
            code="code-123",
            code_verifier="verifier-123",
            nonce="nonce-123",
        )


@pytest.mark.asyncio
async def test_token_signed_by_unknown_key_is_rejected_after_one_refresh() -> None:
    trusted = RSAKey.generate_key(parameters={"kid": "test-key"})
    attacker = RSAKey.generate_key(parameters={"kid": "attacker-key"})
    client = _client(_token(attacker), trusted)
    with pytest.raises(UnauthorizedError):
        await client.exchange_authorization_code(
            code="code-123",
            code_verifier="verifier-123",
            nonce="nonce-123",
        )


@pytest.mark.asyncio
async def test_discovery_cannot_redirect_jwks_to_another_origin() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "issuer": "https://identity.example.test",
                "authorization_endpoint": "https://identity.example.test/login/oauth/authorize",
                "token_endpoint": "https://identity.example.test/api/login/oauth/access_token",
                "jwks_uri": "https://attacker.example.test/jwks",
            },
        )

    client = OidcProviderClient(_settings(), transport=httpx.MockTransport(handler))
    with pytest.raises(ServiceUnavailableError, match="cross-origin"):
        await client.get_metadata()


@pytest.mark.parametrize("algorithm", ["none", "HS256"])
def test_settings_rejects_non_asymmetric_jwt_algorithm(algorithm: str) -> None:
    with pytest.raises(ValueError, match="asymmetric"):
        Settings(
            _env_file=None,
            casdoor_endpoint="https://identity.example.test",
            casdoor_client_id="info-admin-client",
            casdoor_client_secret="test-only-secret",
            casdoor_redirect_uri="https://info.example.test/api/auth/callback",
            frontend_base_url="https://info.example.test",
            auth_allowed_algorithms=algorithm,
        ).auth_allowed_algorithm_list


def test_settings_rejects_wildcard_credential_cors() -> None:
    with pytest.raises(ValueError, match="wildcard"):
        Settings(
            _env_file=None,
            frontend_base_url="https://info.example.test",
            frontend_allowed_origins="*",
        )


@pytest.mark.asyncio
async def test_custom_discovery_url_cannot_send_credentials_cross_origin() -> None:
    settings = _settings().model_copy(
        update={"casdoor_discovery_url": "https://attacker.example.test/.well-known/config"}
    )
    client = OidcProviderClient(settings, transport=httpx.MockTransport(lambda _: httpx.Response(200)))
    with pytest.raises(ServiceUnavailableError, match="cross-origin"):
        await client.get_metadata()
