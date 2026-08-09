from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from app.application.errors.exceptions import ForbiddenError, UnauthorizedError
from app.domain.security import Principal
from app.infrastructure.security.oidc import OidcProviderClient
from core.config import Settings, get_settings


class ServiceIdentityVerifier:
    """Verify a signed workload JWT and enforce exact subject bindings."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        oidc_client: OidcProviderClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._oidc = oidc_client or OidcProviderClient(
            self._settings,
            self._settings.browser_profile("admin"),
        )

    async def verify(
        self, encoded: str, *, required_scopes: frozenset[str]
    ) -> Principal:
        self._settings.require_service_identity()
        if not encoded or len(encoded) > 16384:
            raise UnauthorizedError(
                "The service identity token is invalid", code="token_invalid"
            )
        claims = await self._oidc.verify_access_token(
            encoded,
            audience=self._settings.service_auth_audience,
        )
        subject = claims.get("sub")
        issuer = claims.get("iss")
        if not isinstance(subject, str) or not isinstance(issuer, str):
            raise UnauthorizedError(
                "The service identity token is invalid", code="token_invalid"
            )
        allowed_scopes = self._settings.service_auth_subject_bindings.get(subject)
        if allowed_scopes is None:
            raise ForbiddenError(
                "The service identity is not bound to this Backend",
                code="service_subject_unbound",
            )
        token_scopes = self._scopes(claims)
        if not token_scopes.issubset(allowed_scopes) or not required_scopes.issubset(
            token_scopes
        ):
            raise ForbiddenError(
                "The service identity scope is not authorized",
                code="service_scope_denied",
            )
        return Principal(
            actor_type="service",
            subject=subject,
            issuer=issuer,
            app=self._settings.app_slug,
            surface="internal",
            audience=self._settings.service_auth_audience,
            scopes=token_scopes,
            authenticated_at=self._timestamp(claims.get("iat")),
            expires_at=self._timestamp(claims.get("exp")),
            policy_version="service-v1",
        )

    @staticmethod
    def _timestamp(value: Any) -> datetime:
        if not isinstance(value, (int, float)):
            raise UnauthorizedError(
                "The service identity token is invalid", code="token_invalid"
            )
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError) as exc:
            raise UnauthorizedError(
                "The service identity token is invalid", code="token_invalid"
            ) from exc

    @staticmethod
    def _scopes(claims: dict[str, Any]) -> frozenset[str]:
        raw = claims.get("scope", claims.get("scp", ()))
        if isinstance(raw, str):
            values = re.split(r"\s+", raw.strip()) if raw.strip() else []
        elif isinstance(raw, list) and all(isinstance(item, str) for item in raw):
            values = raw
        else:
            raise UnauthorizedError(
                "The service identity token has invalid scopes",
                code="token_invalid",
            )
        scopes = frozenset(item.strip() for item in values if item.strip())
        if (
            len(scopes) != len(values)
            or len(scopes) > 128
            or any(len(item) > 128 for item in scopes)
        ):
            raise UnauthorizedError(
                "The service identity token has invalid scopes",
                code="token_invalid",
            )
        return scopes
