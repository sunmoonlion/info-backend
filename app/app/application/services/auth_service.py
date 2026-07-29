from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from datetime import UTC, datetime
from typing import Any

from pydantic import ValidationError as PydanticValidationError
from sqlalchemy import text

from app.application.errors.exceptions import ForbiddenError, UnauthorizedError
from app.domain.security import BrowserSession, LoginStart, Principal, SessionStart
from app.infrastructure.security import OidcProviderClient
from app.infrastructure.storage.postgres import get_postgres
from app.infrastructure.storage.redis import get_redis
from core.config import Settings, get_settings

_DEFAULT_SETTINGS = get_settings()
SESSION_COOKIE = _DEFAULT_SETTINGS.session_cookie_name
TRANSACTION_COOKIE = _DEFAULT_SETTINGS.transaction_cookie_name
ADMIN_SCOPE = _DEFAULT_SETTINGS.required_admin_scope
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class AuthService:
    def __init__(
        self,
        settings: Settings | None = None,
        oidc_client: OidcProviderClient | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        self._oidc = oidc_client or OidcProviderClient(self._settings)

    @staticmethod
    def _normalize_return_to(value: str | None) -> str:
        if not value or not value.startswith("/") or value.startswith("//"):
            return "/"
        if "\\" in value or "\x00" in value:
            return "/"
        return value[:1024]

    @staticmethod
    def _new_secret(bytes_count: int = 32) -> str:
        return secrets.token_urlsafe(bytes_count)

    @staticmethod
    def _pkce_challenge(verifier: str) -> str:
        digest = hashlib.sha256(verifier.encode("ascii")).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")

    async def begin_login(self, return_to: str | None = None) -> LoginStart:
        transaction_id = self._new_secret()
        state = self._new_secret()
        nonce = self._new_secret()
        code_verifier = self._new_secret(64)
        transaction = {
            "state": state,
            "nonce": nonce,
            "code_verifier": code_verifier,
            "return_to": self._normalize_return_to(return_to),
            "created_at": int(time.time()),
        }
        key = f"{self._settings.transaction_key_prefix}{transaction_id}"
        stored = await get_redis().client.set(
            key,
            json.dumps(transaction, separators=(",", ":")),
            ex=self._settings.auth_transaction_ttl_seconds,
            nx=True,
        )
        if not stored:
            raise UnauthorizedError("OIDC transaction could not be created")
        try:
            authorization_url = await self._oidc.build_authorization_url(
                state=state,
                nonce=nonce,
                code_challenge=self._pkce_challenge(code_verifier),
            )
        except Exception:
            await get_redis().client.delete(key)
            raise
        return LoginStart(
            authorization_url=authorization_url,
            transaction_id=transaction_id,
        )

    async def complete_login(
        self,
        *,
        code: str,
        state: str,
        transaction_id: str | None,
    ) -> tuple[SessionStart, str]:
        if not transaction_id or len(transaction_id) > 256:
            raise UnauthorizedError("OIDC transaction invalid")
        key = f"{self._settings.transaction_key_prefix}{transaction_id}"
        raw = await get_redis().client.getdel(key)
        if not raw:
            raise UnauthorizedError("OIDC transaction invalid")
        try:
            transaction = json.loads(raw)
            expected_state = transaction["state"]
            nonce = transaction["nonce"]
            code_verifier = transaction["code_verifier"]
            return_to = self._normalize_return_to(transaction.get("return_to"))
        except (KeyError, TypeError, json.JSONDecodeError) as exc:
            raise UnauthorizedError("OIDC transaction invalid") from exc
        if not isinstance(expected_state, str) or not hmac.compare_digest(
            expected_state, state
        ):
            raise UnauthorizedError("OIDC state mismatch")
        if not isinstance(nonce, str) or not isinstance(code_verifier, str):
            raise UnauthorizedError("OIDC transaction invalid")

        claims = await self._oidc.exchange_authorization_code(
            code=code,
            code_verifier=code_verifier,
            nonce=nonce,
        )
        principal = await self._principal_from_verified_claims(claims)
        session_id = self._new_secret()
        browser_session = BrowserSession(
            principal=principal,
            csrf_token=self._new_secret(),
        )
        ttl = max(1, int((principal.expires_at - datetime.now(UTC)).total_seconds()))
        stored = await get_redis().client.set(
            f"{self._settings.session_key_prefix}{session_id}",
            browser_session.model_dump_json(),
            ex=ttl,
            nx=True,
        )
        if not stored:
            raise UnauthorizedError("Session could not be created")
        return (
            SessionStart(session_id=session_id, expires_at=principal.expires_at),
            return_to,
        )

    async def _principal_from_verified_claims(
        self, claims: dict[str, Any]
    ) -> Principal:
        issuer = str(claims["iss"])
        subject = str(claims["sub"])
        provider_exp = int(claims["exp"])
        issued_at = int(claims["iat"])
        now = int(time.time())
        expires_at_epoch = min(provider_exp, now + self._settings.session_ttl_seconds)
        if expires_at_epoch <= now:
            raise UnauthorizedError("OIDC token expired")
        local_user = await self._load_or_create_user(issuer, subject, claims)
        return Principal(
            actor_type="user",
            subject=subject,
            issuer=issuer,
            app=self._settings.app_slug,
            surface="admin",
            audience=self._settings.casdoor_client_id,
            actor_id=local_user["id"],
            display_name=local_user.get("display_name"),
            email=local_user.get("email"),
            roles=tuple(local_user.get("roles") or ()),
            scopes=frozenset(local_user.get("scopes") or ()),
            authenticated_at=datetime.fromtimestamp(issued_at, tz=UTC),
            expires_at=datetime.fromtimestamp(expires_at_epoch, tz=UTC),
            policy_version=self._settings.auth_policy_version,
        )

    async def _load_or_create_user(
        self,
        issuer: str,
        subject: str,
        claims: dict[str, Any],
    ) -> dict[str, Any]:
        username = str(
            claims.get("preferred_username")
            or claims.get("name")
            or claims.get("email")
            or subject
        ).strip()[:255]
        email = str(claims.get("email") or "").strip()[:320] or None
        display_name = str(claims.get("name") or username).strip()[:256] or None
        roles = self._allowed_claims(
            (claims.get("roles"), claims.get("role")),
            self._settings.auth_role_allowlist_items,
        )
        scopes = self._allowed_claims(
            (
                claims.get("scope"),
                claims.get("scp"),
                claims.get("permissions"),
            ),
            self._settings.auth_scope_allowlist_items,
        )
        statement = text(
            """
            INSERT INTO auth_user (
                id, issuer, subject, username, email, display_name, roles, scopes
            )
            VALUES (
                :id, :issuer, :subject, :username, :email, :display_name,
                CAST(:roles AS jsonb), CAST(:scopes AS jsonb)
            )
            ON CONFLICT (issuer, subject) DO UPDATE SET
                username = EXCLUDED.username,
                email = EXCLUDED.email,
                display_name = EXCLUDED.display_name,
                roles = EXCLUDED.roles,
                scopes = EXCLUDED.scopes,
                updated_at = NOW()
            RETURNING id, email, display_name, roles, scopes
            """
        )
        async with get_postgres().session_factory() as session:
            result = await session.execute(
                statement,
                {
                    "id": uuid.uuid4(),
                    "issuer": issuer,
                    "subject": subject,
                    "username": username,
                    "email": email,
                    "display_name": display_name,
                    "roles": json.dumps(roles, separators=(",", ":")),
                    "scopes": json.dumps(scopes, separators=(",", ":")),
                },
            )
            row = result.mappings().one()
            await session.commit()
        return dict(row)

    @staticmethod
    def _allowed_claims(
        values: tuple[object, ...],
        allowlist: frozenset[str],
    ) -> list[str]:
        selected: set[str] = set()
        for value in values:
            candidates: list[Any]
            if isinstance(value, (list, tuple, set, frozenset)):
                candidates = list(value)
            elif isinstance(value, str):
                normalized = value.strip()
                if not normalized:
                    continue
                try:
                    decoded = json.loads(normalized)
                except json.JSONDecodeError:
                    decoded = None
                if isinstance(decoded, list):
                    candidates = decoded
                else:
                    candidates = re.split(r"[,\s]+", normalized)
            else:
                continue
            for candidate in candidates:
                item = str(candidate).strip()
                if item in allowlist:
                    selected.add(item)
        return sorted(selected)

    async def get_browser_session(
        self, session_id: str | None
    ) -> BrowserSession | None:
        if not session_id or len(session_id) > 256:
            return None
        key = f"{self._settings.session_key_prefix}{session_id}"
        raw = await get_redis().client.get(key)
        if not raw:
            return None
        try:
            session = BrowserSession.model_validate_json(raw)
        except PydanticValidationError:
            await get_redis().client.delete(key)
            return None
        principal = session.principal
        if principal.expires_at <= datetime.now(UTC):
            await get_redis().client.delete(key)
            return None
        if (
            principal.app != self._settings.app_slug
            or principal.surface != "admin"
            or principal.audience != self._settings.casdoor_client_id
            or principal.policy_version != self._settings.auth_policy_version
        ):
            await get_redis().client.delete(key)
            return None
        return session

    async def delete_session(self, session_id: str | None) -> None:
        if session_id and len(session_id) <= 256:
            await get_redis().client.delete(
                f"{self._settings.session_key_prefix}{session_id}"
            )

    def validate_csrf(
        self,
        *,
        session: BrowserSession,
        method: str,
        origin: str | None,
        csrf_token: str | None,
    ) -> None:
        if method.upper() in SAFE_METHODS:
            return
        if not origin or origin not in self._settings.frontend_origin_list:
            raise ForbiddenError("Request origin denied")
        if not csrf_token or not hmac.compare_digest(session.csrf_token, csrf_token):
            raise ForbiddenError("CSRF validation failed")

    @staticmethod
    def require_scopes(
        principal: Principal, required: set[str] | frozenset[str]
    ) -> None:
        if not principal.has_scopes(required):
            raise ForbiddenError("Required scope missing")
