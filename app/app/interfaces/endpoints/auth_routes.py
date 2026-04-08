import logging

from fastapi import APIRouter, Cookie, Response
from fastapi.responses import RedirectResponse

from app.application.services.auth_service import AuthService
from core.config import get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["认证模块"])

SESSION_COOKIE = "session_id"
_auth_service = AuthService()


@router.get("/login", summary="发起 OIDC 登录")
async def login():
    """跳转到 Casdoor 登录页"""
    auth_url = _auth_service.build_auth_url()
    return RedirectResponse(url=auth_url)


@router.get("/callback", summary="OIDC 回调")
async def callback(code: str, response: Response):
    """用 code 换 token，存 session，下发 HTTP-only cookie"""
    settings = get_settings()
    try:
        session_id = await _auth_service.exchange_code_for_session(code)
        redirect = RedirectResponse(url=f"{settings.frontend_base_url}/#/")
        redirect.set_cookie(
            key=SESSION_COOKIE,
            value=session_id,
            httponly=True,
            secure=settings.env == "production",
            samesite="lax",
            max_age=settings.session_ttl_seconds,
        )
        return redirect
    except Exception as e:
        logger.error(f"OIDC callback 失败: {e}")
        return RedirectResponse(url=f"{settings.frontend_base_url}/#/login?error=auth_failed")


@router.get("/logout", summary="退出登录")
async def logout(session_id: str | None = Cookie(default=None)):
    """清除 session，跳转登录页"""
    if session_id:
        await _auth_service.delete_session(session_id)
    redirect = RedirectResponse(url=f"{get_settings().frontend_base_url}/#/login")
    redirect.delete_cookie(SESSION_COOKIE)
    return redirect


@router.get("/me", summary="获取当前用户")
async def me(session_id: str | None = Cookie(default=None)):
    """从 session 获取当前登录用户信息"""
    if not session_id:
        return {"user": None}
    session = await _auth_service.get_session(session_id)
    return {"user": session}



