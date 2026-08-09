from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from app.infrastructure.security import OidcProviderClient
from core.config import Settings, get_settings


class KnowledgeAppNotConfiguredError(RuntimeError):
    pass


class ServiceTokenProvider:
    """Short-lived, in-memory client-credentials token cache for one relation."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        service_discovery_url = (
            settings.knowledge_app_service_discovery_url
            or settings.casdoor_discovery_endpoint
        )
        service_endpoint = settings.casdoor_endpoint
        if service_discovery_url:
            parsed = urlsplit(service_discovery_url)
            if parsed.scheme in {"http", "https"} and parsed.hostname:
                service_endpoint = urlunsplit(
                    (parsed.scheme, parsed.netloc, "", "", "")
                )
        service_settings = settings.model_copy(
            update={
                "casdoor_endpoint": service_endpoint,
                "casdoor_discovery_url": service_discovery_url,
                "casdoor_backchannel_endpoint": (
                    settings.knowledge_app_service_backchannel_endpoint
                ),
            }
        )
        self._oidc = OidcProviderClient(
            service_settings,
            service_settings.browser_profile("admin"),
        )
        self._access_token: str | None = None
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def get_token(self) -> str:
        client_id = self._settings.knowledge_app_service_client_id
        client_secret = self._settings.knowledge_app_service_client_secret
        if not client_id or not client_secret:
            raise KnowledgeAppNotConfiguredError(
                "Knowledge service client credentials are not configured"
            )
        now = time.time()
        if self._access_token and self._expires_at > now + 30:
            return self._access_token

        async with self._lock:
            now = time.time()
            if self._access_token and self._expires_at > now + 30:
                return self._access_token
            body = await self._oidc.exchange_client_credentials(
                scope=self._settings.knowledge_app_service_scope,
                client_id=client_id,
                client_secret=client_secret,
            )
            access_token = body.get("access_token") if isinstance(body, dict) else None
            if not isinstance(access_token, str) or not access_token:
                raise RuntimeError("Knowledge service token missing")
            expires_in = body.get("expires_in", 300)
            if not isinstance(expires_in, int | float) or expires_in <= 0:
                raise RuntimeError("Knowledge service token expiry invalid")
            self._access_token = access_token
            self._expires_at = time.time() + float(expires_in)
            return access_token


@dataclass
class KnowledgeAppClient:
    ingest_url: str | None
    token_provider: ServiceTokenProvider | None
    timeout_seconds: float

    @property
    def enabled(self) -> bool:
        return bool(self.ingest_url and self.token_provider)

    async def ingest_document(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.ingest_url or not self.token_provider:
            raise KnowledgeAppNotConfiguredError(
                "Knowledge service client credentials or ingest URL are not configured"
            )
        token = await self.token_provider.get_token()
        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(
                self.ingest_url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
            )
            response.raise_for_status()
            try:
                body: Any = response.json()
            except ValueError:
                body = response.text
            return {"status_code": response.status_code, "body": body}


@lru_cache(maxsize=1)
def get_knowledge_app_client() -> KnowledgeAppClient:
    settings = get_settings()
    return KnowledgeAppClient(
        ingest_url=settings.knowledge_app_ingest_url,
        token_provider=(
            ServiceTokenProvider(settings)
            if settings.knowledge_app_ingest_enabled
            else None
        ),
        timeout_seconds=settings.knowledge_app_timeout_seconds,
    )
