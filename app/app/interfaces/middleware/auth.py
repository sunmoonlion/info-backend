import logging

from fastapi import Cookie, Depends
from fastapi.security import HTTPBearer

from app.application.errors.exceptions import UnauthorizedError
from app.application.services.auth_service import AuthService

logger = logging.getLogger(__name__)

_auth_service = AuthService()

# 公开路由前缀，无需验证
PUBLIC_PATHS = {"/api/auth/login", "/api/auth/callback", "/docs", "/openapi.json", "/health"}


async def get_current_user(session_id: str | None = Cookie(default=None)) -> dict:
    """FastAPI 依赖项：验证 session，返回当前用户信息"""
    if not session_id:
        raise UnauthorizedError()
    session = await _auth_service.get_session(session_id)
    if not session:
        raise UnauthorizedError()
    return session
