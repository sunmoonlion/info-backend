from __future__ import annotations

import asyncio
import time
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.application.errors.exceptions import (
    BadRequestError,
    ServiceUnavailableError,
)
from app.infrastructure.security import OidcProviderClient
from core.config import Settings, get_settings


class DownstreamServiceClient:
    """Server-only OAuth client restricted to a different App boundary."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        oidc_client: OidcProviderClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._oidc = oidc_client or OidcProviderClient(
            self._settings, self._settings.browser_profile("web")
        )
        self._transport = transport
        self._access_token: str | None = None
        self._access_token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    def _url(self, path: str) -> str:
        if (
            not path.startswith("/")
            or path.startswith("//")
            or "\\" in path
            or "\x00" in path
        ):
            raise BadRequestError(
                "The downstream route is invalid", code="downstream_route_denied"
            )
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise BadRequestError(
                "The downstream route is invalid", code="downstream_route_denied"
            )
        allowed = self._settings.downstream_allowed_path_prefix_list
        if not any(
            parsed.path == prefix or parsed.path.startswith(f"{prefix}/")
            for prefix in allowed
        ):
            raise BadRequestError(
                "The downstream route is not allowed",
                code="downstream_route_denied",
            )
        return f"{self._settings.downstream_base_url.rstrip('/')}{path}"

    async def _token(self) -> str:
        if (
            self._access_token is not None
            and self._access_token_expires_at > time.monotonic() + 30
        ):
            return self._access_token
        async with self._token_lock:
            if (
                self._access_token is not None
                and self._access_token_expires_at > time.monotonic() + 30
            ):
                return self._access_token
            self._settings.require_downstream_identity()
            token = await self._oidc.exchange_client_credentials(
                scope=self._settings.downstream_scope,
                client_id=self._settings.downstream_client_id,
                client_secret=self._settings.downstream_client_secret,
            )
            access_token = token.get("access_token")
            if not isinstance(access_token, str) or not access_token:
                raise ServiceUnavailableError(
                    "The downstream identity provider returned an invalid token"
                )
            try:
                ttl = max(1, min(int(token.get("expires_in", 300)), 3600))
            except (TypeError, ValueError):
                ttl = 300
            self._access_token = access_token
            self._access_token_expires_at = time.monotonic() + ttl
            return access_token

    async def request_json(
        self,
        method: str,
        path: str,
        *,
        operation_id: str,
        json_body: dict[str, Any] | None = None,
    ) -> Any:
        url = self._url(path)
        token = await self._token()
        try:
            async with httpx.AsyncClient(
                verify=self._settings.downstream_verify_ssl,
                timeout=self._settings.downstream_http_timeout_seconds,
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = await client.request(
                    method.upper(),
                    url,
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {token}",
                        "X-Operation-ID": operation_id,
                    },
                    json=json_body,
                )
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError(
                "The downstream service is unavailable"
            ) from exc
        if response.status_code >= 500:
            raise ServiceUnavailableError("The downstream service is unavailable")
        if response.status_code >= 400:
            raise BadRequestError(
                "The downstream request was rejected",
                code="downstream_request_rejected",
            )
        try:
            return response.json()
        except ValueError as exc:
            raise ServiceUnavailableError(
                "The downstream service returned an invalid response",
                code="contract_invalid",
            ) from exc
