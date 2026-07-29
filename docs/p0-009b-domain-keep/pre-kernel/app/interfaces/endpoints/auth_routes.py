from __future__ import annotations

import logging
from datetime import UTC, datetime

from fastapi import APIRouter, Cookie, Depends, Query, Response
from fastapi.responses import RedirectResponse

from app.application.errors.exceptions import AppException
from app.application.services.auth_service import (
    SESSION_COOKIE,
    TRANSACTION_COOKIE,
    AuthService,
)
from app.domain.security import BrowserSession
from app.interfaces.middleware.auth import get_current_browser_session
from core.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["认证模块"])
_auth_service = AuthService()


def _frontend_redirect(path: str) -> str:
    return f"{get_settings().frontend_base_url.rstrip('/')}{path}"


def _set_cookie(response: Response, *, key: str, value: str, max_age: int) -> None:
    settings = get_settings()
    response.set_cookie(
        key=key,
        value=value,
        httponly=True,
        secure=settings.auth_cookie_secure,
        samesite="lax",
        max_age=max_age,
        path="/",
    )


@router.get("/login", summary="发起 OIDC 登录")
async def login(return_to: str | None = Query(default=None)) -> RedirectResponse:
    start = await _auth_service.begin_login(return_to)
    redirect = RedirectResponse(url=start.authorization_url, status_code=302)
    _set_cookie(
        redirect,
        key=TRANSACTION_COOKIE,
        value=start.transaction_id,
        max_age=get_settings().auth_transaction_ttl_seconds,
    )
    return redirect


@router.get("/callback", summary="OIDC 回调")
async def callback(
    code: str,
    state: str,
    transaction_id: str | None = Cookie(default=None, alias=TRANSACTION_COOKIE),
) -> RedirectResponse:
    try:
        session, return_to = await _auth_service.complete_login(
            code=code,
            state=state,
            transaction_id=transaction_id,
        )
    except AppException as exc:
        logger.warning("OIDC callback rejected: status=%s", exc.status_code)
        redirect = RedirectResponse(
            url=_frontend_redirect("/login?error=auth_failed"),
            status_code=302,
        )
        redirect.delete_cookie(TRANSACTION_COOKIE, path="/")
        return redirect
    redirect = RedirectResponse(url=_frontend_redirect(return_to), status_code=302)
    max_age = max(
        1,
        int((session.expires_at - datetime.now(UTC)).total_seconds()),
    )
    _set_cookie(redirect, key=SESSION_COOKIE, value=session.session_id, max_age=max_age)
    redirect.delete_cookie(TRANSACTION_COOKIE, path="/")
    return redirect


@router.post("/logout", status_code=204, summary="退出登录")
async def logout(
    response: Response,
    session_id: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    _: BrowserSession = Depends(get_current_browser_session),
) -> None:
    await _auth_service.delete_session(session_id)
    response.delete_cookie(SESSION_COOKIE, path="/")


@router.get("/me", summary="获取当前用户")
async def me(session: BrowserSession = Depends(get_current_browser_session)) -> dict:
    principal = session.principal
    return {
        "contract_version": 1,
        "authenticated": True,
        "user": {
            "actor_id": str(principal.actor_id),
            "app": principal.app,
            "surface": principal.surface,
            "display_name": principal.display_name,
            "email": principal.email,
            "roles": list(principal.roles),
            "scopes": sorted(principal.scopes),
            "expires_at": principal.expires_at.isoformat(),
        },
        "csrf_token": session.csrf_token,
    }
