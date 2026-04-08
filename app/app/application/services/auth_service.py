import json
import logging
import uuid
from urllib.parse import urlencode

import httpx

from app.application.errors.exceptions import UnauthorizedError
from app.infrastructure.storage.redis import get_redis
from core.config import get_settings

logger = logging.getLogger(__name__)

SESSION_PREFIX = "session:"


class AuthService:
    def __init__(self):
        self._settings = get_settings()

    def build_auth_url(self) -> str:
        """构造 Casdoor 授权跳转 URL"""
        params = urlencode({
            "response_type": "code",
            "client_id": self._settings.casdoor_client_id,
            "redirect_uri": self._settings.casdoor_redirect_uri,
            "scope": "openid profile email",
            "state": str(uuid.uuid4()),
        })
        return f"{self._settings.casdoor_endpoint}/login/oauth/authorize?{params}"

    async def exchange_code_for_session(self, code: str) -> str:
        """用 code 换 token，存入 Redis，返回 session_id"""
        async with httpx.AsyncClient(verify=self._settings.casdoor_verify_ssl) as client:
            resp = await client.post(
                f"{self._settings.casdoor_endpoint}/api/login/oauth/access_token",
                data={
                    "grant_type": "authorization_code",
                    "client_id": self._settings.casdoor_client_id,
                    "client_secret": self._settings.casdoor_client_secret,
                    "code": code,
                    "redirect_uri": self._settings.casdoor_redirect_uri,
                },
            )
        if resp.status_code != 200:
            raise UnauthorizedError("Casdoor token 换取失败")

        tokens = resp.json()
        session_id = str(uuid.uuid4())
        redis = get_redis().client
        await redis.set(
            f"{SESSION_PREFIX}{session_id}",
            json.dumps(tokens),
            ex=self._settings.session_ttl_seconds,
        )
        return session_id

    async def get_session(self, session_id: str) -> dict | None:
        """从 Redis 取 session"""
        raw = await get_redis().client.get(f"{SESSION_PREFIX}{session_id}")
        if not raw:
            return None
        return json.loads(raw)

    async def delete_session(self, session_id: str) -> None:
        """删除 session（退出登录）"""
        await get_redis().client.delete(f"{SESSION_PREFIX}{session_id}")

