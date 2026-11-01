"""凭本人令牌调 knowbase（SDD 0042；0045 第 2.4 节、0043 第十节）。

本人在 knowfeed web端亲手提需求、建订阅时，knowfeed 后端把这个会话的访问令牌按 RFC 8693
换成 `aud` 是 knowbase 的令牌（`get_web_knowbase_token`），连同请求一起送去：

- 记确认：`POST /confirmations`（本人亲手提的提交即确认，knowfeed 凭本人令牌请 knowbase
  记下）。knowbase 现在只让 knowfeed 代办的令牌进订阅的几个接口，确认接口要它放开，写在
  交回报告里；放开之前这里回 403 `user_token_app_denied`，照实转给页面。
- 订阅范围同意：建、续 `PUT /subscriptions/{订阅}/revisions/{修订}`，撤
  `DELETE` 同一路径（knowbase 已认 knowfeed 代办的令牌）。

地址 `KNOWBASE_INTERNAL_URL`（必须是 https），同服务关系那几条。令牌只在内存里，不进日志。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from app.domain.supply.models import SupplyError

BASE = "/api/internal/v2/user-data"


@dataclass
class HttpKnowbaseUser:
    environ: Mapping[str, str] | None = None
    timeout_seconds: float = 30.0
    transport: httpx.AsyncBaseTransport | None = None

    def _base(self) -> str:
        env = os.environ if self.environ is None else self.environ
        url = env.get("KNOWBASE_INTERNAL_URL", "").strip()
        if urlsplit(url).scheme != "https":
            raise SupplyError(503, "knowbase_unconfigured", "knowbase is not set up")
        return url.rstrip("/") + BASE

    async def confirm(
        self,
        token: str,
        *,
        kind: str,
        items: list[dict[str, object]],
        supply: dict[str, object],
    ) -> dict[str, Any]:
        return await self._send(
            "POST",
            "/confirmations",
            token,
            json={"kind": kind, "items": items, "supply": supply},
        )

    async def subscribe(
        self, token: str, subscription_id: str, revision: int, scope: dict[str, object]
    ) -> dict[str, Any]:
        return await self._send(
            "PUT", self._subscription(subscription_id, revision), token, json=scope
        )

    async def unsubscribe(
        self, token: str, subscription_id: str, revision: int
    ) -> None:
        try:
            await self._send(
                "DELETE", self._subscription(subscription_id, revision), token
            )
        except SupplyError as exc:
            if exc.status != 404:  # 已经撤了（比如本人在 knowbase 撤过）也算撤了
                raise

    @staticmethod
    def _subscription(subscription_id: str, revision: int) -> str:
        return f"/subscriptions/{quote(subscription_id, safe='')}/revisions/{int(revision)}"

    async def _send(
        self, method: str, path: str, token: str, *, json: Any = None
    ) -> Any:
        try:
            async with httpx.AsyncClient(
                timeout=self.timeout_seconds,
                follow_redirects=False,
                transport=self.transport,
                trust_env=False,  # 集群内部调用，不走环境里的代理
            ) as client:
                response = await client.request(
                    method,
                    self._base() + path,
                    json=json,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                    },
                )
        except httpx.HTTPError:
            raise SupplyError(
                503, "knowbase_unavailable", "knowbase unreachable"
            ) from None
        if response.status_code >= 500:
            raise SupplyError(503, "knowbase_unavailable", "knowbase failed")
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = None
            code = body.get("code") if isinstance(body, dict) else None
            raise SupplyError(
                response.status_code,
                str(code or "knowbase_refused"),
                "knowbase refused",
            )
        if response.status_code == 204 or not response.content:
            return {}
        return response.json()
