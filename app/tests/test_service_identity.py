from __future__ import annotations

import httpx
import pytest

import app.infrastructure.external.knowledge_app as knowledge_app
from app.infrastructure.external.knowledge_app import (
    KnowledgeAppClient,
    KnowledgeAppNotConfiguredError,
    ServiceTokenProvider,
)
from core.config import Settings


class FakeTokenProvider:
    def __init__(self, token: str = "service-token") -> None:
        self.token = token
        self.calls = 0

    async def get_token(self) -> str:
        self.calls += 1
        return self.token


@pytest.mark.asyncio
async def test_knowledge_client_sends_relation_scoped_bearer_token(monkeypatch) -> None:
    provider = FakeTokenProvider()
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers["authorization"]
        assert "api-key" not in {key.lower() for key in request.headers}
        return httpx.Response(202, json={"status": "accepted"})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(
        knowledge_app.httpx,
        "AsyncClient",
        lambda *args, **kwargs: real_client(
            *args, transport=httpx.MockTransport(handler), **kwargs
        ),
    )
    client = KnowledgeAppClient(
        ingest_url="https://knowledge.example.test/api/internal/v1/knowledge/ingestions",
        token_provider=provider,
        timeout_seconds=1.0,
    )

    result = await client.ingest_document({"contract_version": 1})

    assert result["status_code"] == 202
    assert seen["authorization"] == "Bearer service-token"
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_knowledge_client_fails_closed_without_service_credentials() -> None:
    client = KnowledgeAppClient(ingest_url="https://knowledge.example.test/ingest", token_provider=None, timeout_seconds=1.0)

    with pytest.raises(KnowledgeAppNotConfiguredError):
        await client.ingest_document({"contract_version": 1})


def test_service_token_provider_uses_dedicated_discovery_and_backchannel() -> None:
    settings = Settings(
        _env_file=None,
        casdoor_endpoint="https://browser-identity.example.test",
        casdoor_backchannel_endpoint="http://browser-casdoor:8000",
        KNOWLEDGE_APP_SERVICE_DISCOVERY_URL=(
            "https://service-identity.example.test/.well-known/openid-configuration"
        ),
        KNOWLEDGE_APP_SERVICE_BACKCHANNEL_ENDPOINT="http://service-casdoor:8000",
        KNOWLEDGE_APP_SERVICE_CLIENT_ID="service-client",
        KNOWLEDGE_APP_SERVICE_CLIENT_SECRET="service-secret",
    )

    provider = ServiceTokenProvider(settings)

    assert provider._oidc._settings.casdoor_endpoint == (
        "https://service-identity.example.test"
    )
    assert provider._oidc._settings.casdoor_discovery_url == (
        "https://service-identity.example.test/.well-known/openid-configuration"
    )
    assert provider._oidc._settings.casdoor_backchannel_endpoint == (
        "http://service-casdoor:8000"
    )
