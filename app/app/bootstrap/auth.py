"""Wires browser authentication to its OIDC, Redis and PostgreSQL implementations."""

from __future__ import annotations

from app.application.services.auth_service import AuthService
from app.infrastructure.repositories.auth_user import SqlUserDirectory
from app.infrastructure.security import OidcProviderClient
from app.infrastructure.storage.session_store import RedisSessionStore
from core.config import BrowserSurface, Settings, get_settings


def build_auth_service(
    surface: BrowserSurface, settings: Settings | None = None
) -> AuthService:
    runtime_settings = settings or get_settings()
    return AuthService(
        surface,
        runtime_settings,
        OidcProviderClient(runtime_settings, runtime_settings.browser_profile(surface)),
        sessions=RedisSessionStore(),
        users=SqlUserDirectory(),
    )
