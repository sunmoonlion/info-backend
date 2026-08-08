from __future__ import annotations

from datetime import UTC, datetime

from fastapi import Response
from fastapi.responses import RedirectResponse

from app.domain.security import BrowserSession
from core.config import BrowserSurfaceProfile, Settings


def frontend_redirect(profile: BrowserSurfaceProfile, path: str) -> str:
    return f"{profile.frontend_base_url.rstrip('/')}{path}"


def set_browser_cookie(
    response: Response,
    *,
    settings: Settings,
    key: str,
    value: str,
    max_age: int,
) -> None:
    response.set_cookie(
        key=key,
        value=value,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        max_age=max_age,
        path="/",
    )


def set_session_cookie(
    response: RedirectResponse,
    *,
    settings: Settings,
    profile: BrowserSurfaceProfile,
    session: BrowserSession | object,
    session_id: str,
    expires_at: datetime,
) -> None:
    del session
    max_age = max(1, int((expires_at - datetime.now(UTC)).total_seconds()))
    set_browser_cookie(
        response,
        settings=settings,
        key=profile.session_cookie_name,
        value=session_id,
        max_age=max_age,
    )
    response.delete_cookie(profile.transaction_cookie_name, path="/")
    response.headers["Cache-Control"] = "no-store"
