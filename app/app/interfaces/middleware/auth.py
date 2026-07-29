from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import Cookie, Depends, Header, Request

from app.application.audit_context import set_actor
from app.application.errors.exceptions import UnauthorizedError
from app.application.services.auth_service import (
    ADMIN_SCOPE,
    SESSION_COOKIE,
    AuthService,
)
from app.domain.security import BrowserSession, Principal

_auth_service = AuthService()


async def get_current_browser_session(
    request: Request,
    session_id: str | None = Cookie(default=None, alias=SESSION_COOKIE),
    csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
) -> BrowserSession:
    session = await _auth_service.get_browser_session(session_id)
    if session is None or session.principal.actor_type != "user":
        raise UnauthorizedError()
    _auth_service.validate_csrf(
        session=session,
        method=request.method,
        origin=request.headers.get("origin"),
        csrf_token=csrf_token,
    )
    set_actor(str(session.principal.actor_id))
    return session


async def get_current_user(
    session: BrowserSession = Depends(get_current_browser_session),
) -> Principal:
    # Compatibility entry point for existing imports. New routes should depend on
    # get_current_browser_session or a scope dependency directly.
    return session.principal


def require_scopes(*required: str) -> Callable[..., Awaitable[Principal]]:
    required_set = frozenset(required)

    async def dependency(
        request: Request,
        session_id: str | None = Cookie(default=None, alias=SESSION_COOKIE),
        csrf_token: str | None = Header(default=None, alias="X-CSRF-Token"),
    ) -> Principal:
        session = await _auth_service.get_browser_session(session_id)
        if session is None or session.principal.actor_type != "user":
            raise UnauthorizedError()
        _auth_service.validate_csrf(
            session=session,
            method=request.method,
            origin=request.headers.get("origin"),
            csrf_token=csrf_token,
        )
        _auth_service.require_scopes(session.principal, required_set)
        set_actor(str(session.principal.actor_id))
        return session.principal

    return dependency


require_info_admin = require_scopes(ADMIN_SCOPE)
