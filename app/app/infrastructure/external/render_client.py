"""渲染工作负载的客户端（SDD 0045 第 3.2 节「无头浏览器放 knowfeed 自己的一个单独工作负载」；
decisions 10-29「发版前补齐」④：工作负载与出网规则由平台起，交 Fable）。

knowfeed 自己不带浏览器内核：要渲染的网页交给集群里那个单独的工作负载。它不挂密钥、只许
出公网；页面的子资源与后台请求也都经受控出口、同样拒内部地址；每次渲染隔离 Cookie 与
下载目录，页面与附件总量、时间、内存设上限。这些由那个工作负载守，这里先核一遍网址是公网。

接口（交平台起服务时照这个做）：
  POST {SUPPLY_RENDER_URL}/render
  请求 {"url": "...", "max_bytes": 10485760, "timeout_seconds": 30}
  回 200 {"status": 200, "final_url": "...", "content_type": "text/html; charset=utf-8",
          "body_base64": "..."}；页面本身的状态照实放在 status 里。
服务在集群内部，不经公网，不带凭据（它不挂密钥，也不认谁调它；网络策略只让 knowfeed 的
worker 连它）。`SUPPLY_RENDER_URL` 没配就是没有渲染：要渲染的渠道照实回 render_unavailable。
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass

import httpx

from app.application.ports.supply import Page
from app.domain.supply.models import SupplyError
from app.infrastructure.external.crawl_http import CrawlFetchError, ensure_public_url


@dataclass
class HttpRenderer:
    base_url: str
    timeout_seconds: float
    transport: httpx.AsyncBaseTransport | None = None  # 测试换成记录请求的

    def configured(self) -> bool:
        return bool(self.base_url.strip())

    async def render(self, url: str, *, max_bytes: int, timeout_seconds: float) -> Page:
        if not self.configured():
            raise SupplyError(422, "render_unavailable", "no render service")
        try:
            checked = await ensure_public_url(url)
        except CrawlFetchError as exc:
            raise SupplyError(422, str(exc), "the URL is not public") from None
        wait = min(timeout_seconds, self.timeout_seconds)
        try:
            async with httpx.AsyncClient(
                timeout=wait + 10, transport=self.transport, trust_env=False
            ) as client:
                response = await client.post(
                    self.base_url.rstrip("/") + "/render",
                    json={
                        "url": checked,
                        "max_bytes": max_bytes,
                        "timeout_seconds": wait,
                    },
                    headers={"Accept": "application/json"},
                )
        except httpx.HTTPError:
            raise SupplyError(
                503, "render_failed", "render service unreachable"
            ) from None
        if response.status_code != 200:
            status = 503 if response.status_code >= 500 else 422
            raise SupplyError(status, "render_failed", "render service refused")
        try:
            body = response.json()
            content = base64.b64decode(str(body["body_base64"]), validate=True)
            page = Page(
                status=int(body["status"]),
                media_type=str(body.get("content_type") or "text/html; charset=utf-8"),
                content=content,
                final_url=str(body.get("final_url") or checked),
            )
        except (ValueError, KeyError, TypeError, binascii.Error):
            raise SupplyError(502, "unexpected_response", "render answer") from None
        if len(page.content) > max_bytes:
            raise SupplyError(422, "render_too_large", "the rendered page is too large")
        return page
