from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit, urlunsplit

import httpx
from joserfc import jwt
from joserfc.errors import JoseError
from joserfc.jwk import KeySet
from joserfc.jwt import JWTClaimsRegistry

from app.application.errors.exceptions import ServiceUnavailableError, UnauthorizedError
from core.config import Settings


@dataclass(frozen=True)
class OidcMetadata:
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    jwks_uri: str


class OidcProviderClient:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._metadata: OidcMetadata | None = None
        self._metadata_loaded_at = 0.0
        self._key_set: KeySet | None = None
        self._keys_loaded_at = 0.0

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            verify=self._settings.casdoor_verify_ssl,
            timeout=self._settings.auth_http_timeout_seconds,
            follow_redirects=False,
            transport=self._transport,
        )

    def _validate_provider_url(self, value: Any, field: str) -> str:
        if not isinstance(value, str) or not value:
            raise ServiceUnavailableError(f"OIDC metadata missing {field}")
        parsed = urlsplit(value)
        endpoint = urlsplit(self._settings.casdoor_endpoint)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ServiceUnavailableError(f"OIDC metadata invalid {field}")
        if parsed.username or parsed.password or parsed.fragment:
            raise ServiceUnavailableError(f"OIDC metadata invalid {field}")
        if self._settings.env == "production" and parsed.scheme != "https":
            raise ServiceUnavailableError("OIDC metadata requires HTTPS")
        if not endpoint.hostname or parsed.hostname.lower() != endpoint.hostname.lower():
            raise ServiceUnavailableError(f"OIDC metadata cross-origin {field}")
        if parsed.port != endpoint.port:
            raise ServiceUnavailableError(f"OIDC metadata cross-origin {field}")
        return value

    def _backchannel_target(self, public_url: str, field: str) -> tuple[str, dict[str, str]]:
        public_url = self._validate_provider_url(public_url, field)
        configured = self._settings.casdoor_backchannel_endpoint
        if not configured:
            return public_url, {}
        backchannel = urlsplit(configured)
        if (
            backchannel.scheme not in {"http", "https"}
            or not backchannel.hostname
            or backchannel.username
            or backchannel.password
            or backchannel.path not in {"", "/"}
            or backchannel.query
            or backchannel.fragment
        ):
            raise ServiceUnavailableError("OIDC backchannel endpoint invalid")
        public = urlsplit(public_url)
        endpoint = urlsplit(self._settings.casdoor_endpoint)
        return (
            urlunsplit(
                (
                    backchannel.scheme,
                    backchannel.netloc,
                    public.path,
                    public.query,
                    "",
                )
            ),
            {"Host": endpoint.netloc},
        )

    async def get_metadata(self, *, force_refresh: bool = False) -> OidcMetadata:
        now = time.monotonic()
        if (
            not force_refresh
            and self._metadata is not None
            and now - self._metadata_loaded_at < self._settings.auth_discovery_cache_seconds
        ):
            return self._metadata
        discovery_url = self._settings.casdoor_discovery_endpoint
        if not discovery_url:
            raise ServiceUnavailableError("OIDC discovery is not configured")
        discovery_url, routing_headers = self._backchannel_target(
            discovery_url, "discovery"
        )
        try:
            async with self._client() as client:
                response = await client.get(
                    discovery_url,
                    headers={"Accept": "application/json", **routing_headers},
                )
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError("OIDC discovery unavailable") from exc
        if response.status_code != 200:
            raise ServiceUnavailableError("OIDC discovery unavailable")
        try:
            payload = response.json()
        except ValueError as exc:
            raise ServiceUnavailableError("OIDC discovery returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise ServiceUnavailableError("OIDC discovery returned invalid document")
        metadata = OidcMetadata(
            issuer=self._validate_provider_url(payload.get("issuer"), "issuer"),
            authorization_endpoint=self._validate_provider_url(
                payload.get("authorization_endpoint"), "authorization_endpoint"
            ),
            token_endpoint=self._validate_provider_url(
                payload.get("token_endpoint"), "token_endpoint"
            ),
            jwks_uri=self._validate_provider_url(payload.get("jwks_uri"), "jwks_uri"),
        )
        self._metadata = metadata
        self._metadata_loaded_at = now
        return metadata

    async def _get_key_set(self, metadata: OidcMetadata, *, force_refresh: bool = False) -> KeySet:
        now = time.monotonic()
        if (
            not force_refresh
            and self._key_set is not None
            and now - self._keys_loaded_at < self._settings.auth_jwks_cache_seconds
        ):
            return self._key_set
        try:
            jwks_url, routing_headers = self._backchannel_target(
                metadata.jwks_uri, "jwks_uri"
            )
            async with self._client() as client:
                response = await client.get(
                    jwks_url,
                    headers={"Accept": "application/json", **routing_headers},
                )
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError("OIDC JWKS unavailable") from exc
        if response.status_code != 200:
            raise ServiceUnavailableError("OIDC JWKS unavailable")
        try:
            payload = response.json()
            key_set = KeySet.import_key_set(payload)
        except (ValueError, TypeError, JoseError) as exc:
            raise ServiceUnavailableError("OIDC JWKS invalid") from exc
        self._key_set = key_set
        self._keys_loaded_at = now
        return key_set

    async def build_authorization_url(
        self,
        *,
        state: str,
        nonce: str,
        code_challenge: str,
    ) -> str:
        metadata = await self.get_metadata()
        params = urlencode(
            {
                "response_type": "code",
                "client_id": self._settings.casdoor_client_id,
                "redirect_uri": self._settings.casdoor_redirect_uri,
                "scope": "openid profile email",
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return f"{metadata.authorization_endpoint}?{params}"

    async def exchange_authorization_code(
        self,
        *,
        code: str,
        code_verifier: str,
        nonce: str,
    ) -> dict[str, Any]:
        metadata = await self.get_metadata()
        try:
            token_url, routing_headers = self._backchannel_target(
                metadata.token_endpoint, "token_endpoint"
            )
            async with self._client() as client:
                response = await client.post(
                    token_url,
                    data={
                        "grant_type": "authorization_code",
                        "client_id": self._settings.casdoor_client_id,
                        "client_secret": self._settings.casdoor_client_secret,
                        "code": code,
                        "redirect_uri": self._settings.casdoor_redirect_uri,
                        "code_verifier": code_verifier,
                    },
                    headers={"Accept": "application/json", **routing_headers},
                )
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError("OIDC token endpoint unavailable") from exc
        if response.status_code != 200:
            raise UnauthorizedError("OIDC code exchange failed")
        try:
            token_response = response.json()
        except ValueError as exc:
            raise UnauthorizedError("OIDC token response invalid") from exc
        if not isinstance(token_response, dict):
            raise UnauthorizedError("OIDC token response invalid")
        id_token = token_response.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise UnauthorizedError("OIDC ID token missing")
        return await self.verify_id_token(id_token, nonce=nonce, metadata=metadata)

    async def exchange_client_credentials(self, *, scope: str) -> dict[str, Any]:
        """Request a service token through the same validated OIDC transport.

        Discovery remains authoritative for the public issuer and token URL,
        while ``_backchannel_target`` may replace only the network
        destination.  This prevents service calls from bypassing the provider
        origin checks or losing the public Host header inside Kubernetes.
        """

        metadata = await self.get_metadata()
        try:
            token_url, routing_headers = self._backchannel_target(
                metadata.token_endpoint, "token_endpoint"
            )
            async with self._client() as client:
                response = await client.post(
                    token_url,
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self._settings.casdoor_client_id,
                        "client_secret": self._settings.casdoor_client_secret,
                        "scope": scope,
                    },
                    headers={"Accept": "application/json", **routing_headers},
                )
        except httpx.HTTPError as exc:
            raise ServiceUnavailableError("OIDC token endpoint unavailable") from exc
        if response.status_code != 200:
            raise UnauthorizedError("OIDC client credentials rejected")
        try:
            token_response = response.json()
        except ValueError as exc:
            raise UnauthorizedError("OIDC token response invalid") from exc
        if not isinstance(token_response, dict):
            raise UnauthorizedError("OIDC token response invalid")
        access_token = token_response.get("access_token")
        if not isinstance(access_token, str) or not access_token:
            raise UnauthorizedError("OIDC access token missing")
        return token_response

    async def verify_id_token(
        self,
        encoded: str,
        *,
        nonce: str,
        metadata: OidcMetadata | None = None,
    ) -> dict[str, Any]:
        metadata = metadata or await self.get_metadata()
        last_error: Exception | None = None
        for refresh in (False, True):
            try:
                key_set = await self._get_key_set(metadata, force_refresh=refresh)
                token = jwt.decode(
                    encoded,
                    key_set,
                    algorithms=self._settings.auth_allowed_algorithm_list,
                )
                claims = token.claims
                registry = JWTClaimsRegistry(
                    leeway=self._settings.auth_clock_skew_seconds,
                    iss={"essential": True, "value": metadata.issuer},
                    sub={"essential": True},
                    aud={"essential": True},
                    exp={"essential": True},
                    iat={"essential": True},
                    nonce={"essential": True, "value": nonce},
                )
                registry.validate(claims)
                audience = claims.get("aud")
                if audience != self._settings.casdoor_client_id and audience != [
                    self._settings.casdoor_client_id
                ]:
                    raise UnauthorizedError("OIDC audience mismatch")
                if not isinstance(claims.get("sub"), str) or not claims["sub"].strip():
                    raise UnauthorizedError("OIDC subject invalid")
                return claims
            except UnauthorizedError:
                raise
            except (JoseError, ValueError, TypeError) as exc:
                last_error = exc
        raise UnauthorizedError("OIDC ID token invalid") from last_error
