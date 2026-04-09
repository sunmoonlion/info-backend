import json
import logging
import uuid
import base64
from urllib.parse import urlencode

import httpx
from sqlalchemy import text

from app.application.errors.exceptions import UnauthorizedError
from app.infrastructure.storage.postgres import get_postgres
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
        await self._sync_shadow_user(tokens)
        session_id = str(uuid.uuid4())
        redis = get_redis().client
        await redis.set(
            f"{SESSION_PREFIX}{session_id}",
            json.dumps(tokens),
            ex=self._settings.session_ttl_seconds,
        )
        return session_id

    def _decode_id_token_claims(self, tokens: dict) -> dict:
        """解析 id_token payload（不校验签名，仅用于影子同步字段提取）"""
        id_token = tokens.get("id_token")
        if not isinstance(id_token, str):
            return {}
        try:
            parts = id_token.split(".")
            if len(parts) < 2:
                return {}
            payload = parts[1]
            padding = "=" * (-len(payload) % 4)
            decoded = base64.urlsafe_b64decode(payload + padding).decode("utf-8")
            claims = json.loads(decoded)
            return claims if isinstance(claims, dict) else {}
        except Exception:
            return {}

    async def _sync_shadow_user(self, tokens: dict) -> None:
        """登录成功后同步影子用户（身份仍以 Casdoor 为准）"""
        claims = self._decode_id_token_claims(tokens)
        username = str(
            claims.get("preferred_username")
            or claims.get("name")
            or claims.get("email")
            or claims.get("sub")
            or ""
        ).strip()
        if not username:
            return

        sub = str(claims.get("sub") or "").strip() or None
        email = str(claims.get("email") or "").strip() or None
        full_name = str(claims.get("name") or "").strip() or None

        create_table_sql = text(
            """
            CREATE TABLE IF NOT EXISTS users (
                id UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                username VARCHAR(255) UNIQUE NOT NULL,
                casdoor_sub VARCHAR(255),
                email VARCHAR(255),
                full_name VARCHAR(255),
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        upsert_sql = text(
            """
            INSERT INTO users (username, casdoor_sub, email, full_name)
            VALUES (:username, :casdoor_sub, :email, :full_name)
            ON CONFLICT (username) DO UPDATE SET
                casdoor_sub = EXCLUDED.casdoor_sub,
                email = EXCLUDED.email,
                full_name = EXCLUDED.full_name,
                updated_at = NOW();
            """
        )

        async with get_postgres().session_factory() as session:
            await session.execute(create_table_sql)
            await session.execute(
                upsert_sql,
                {
                    "username": username,
                    "casdoor_sub": sub,
                    "email": email,
                    "full_name": full_name,
                },
            )
            await session.commit()

    async def get_session(self, session_id: str) -> dict | None:
        """从 Redis 取 session"""
        raw = await get_redis().client.get(f"{SESSION_PREFIX}{session_id}")
        if not raw:
            return None
        return json.loads(raw)

    async def delete_session(self, session_id: str) -> None:
        """删除 session（退出登录）"""
        await get_redis().client.delete(f"{SESSION_PREFIX}{session_id}")

