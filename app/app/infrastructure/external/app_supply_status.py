"""把需求状态通知交给提单的应用（契约 knowfeed-supply-status/v1，SDD 0045 第 2.5 节）。

每个接收的应用一条关系 `supply-status`，knowfeed 用这条关系自己的服务身份（读法同
`knowbase_supply.py`）：`<应用>_INTERNAL_URL` 加 `<应用>_SUPPLY_STATUS_SERVICE_{CLIENT_ID,
CLIENT_SECRET,SCOPE,DISCOVERY_URL,BACKCHANNEL_ENDPOINT}`，scope 是 `<应用>:supply-status`，
内部地址要 https；缺一项就是这个应用没开，通知跳过（接收端还没做时就是这样）。

对方 5xx、连不上抛错，由可靠投递退避重试；4xx 也抛（对方说不收，留在死信里给人看）。
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

import httpx

from app.domain.supply.status_event import RECEIVER_PATH
from app.infrastructure.external.knowbase_app import ServiceTokenProvider
from app.infrastructure.external.knowbase_supply import supply_settings
from core.config import get_settings

APPS = ("knowbench", "knowlab")


class StatusNotDelivered(RuntimeError):
    """对方没收下；不带对方回的正文（可能有内部细节），只带状态码。"""


def status_relation(
    app: str, environ: Mapping[str, str] | None = None
) -> dict[str, str] | None:
    if app not in APPS:
        return None
    env = os.environ if environ is None else environ
    upper = app.upper()
    prefix = f"{upper}_SUPPLY_STATUS_SERVICE_"
    values = {
        "internal_url": env.get(f"{upper}_INTERNAL_URL", "").strip(),
        "client_id": env.get(prefix + "CLIENT_ID", "").strip(),
        "client_secret": env.get(prefix + "CLIENT_SECRET", "").strip(),
        "scope": env.get(prefix + "SCOPE", "").strip(),
        "discovery_url": env.get(prefix + "DISCOVERY_URL", "").strip(),
        "backchannel_endpoint": env.get(prefix + "BACKCHANNEL_ENDPOINT", "").strip(),
    }
    if not all(values.values()) or values["scope"] != f"{app}:supply-status":
        return None
    if urlsplit(values["internal_url"]).scheme != "https":
        return None
    return values


@dataclass
class HttpStatusSender:
    timeout_seconds: float = 10.0
    environ: Mapping[str, str] | None = None
    transport: httpx.AsyncBaseTransport | None = None  # 测试换成记录请求的

    def __post_init__(self) -> None:
        self._tokens: dict[str, ServiceTokenProvider] = {}

    def configured(self, app: str) -> bool:
        return status_relation(app, self.environ) is not None

    async def send(self, app: str, body: dict[str, object]) -> None:
        relation = status_relation(app, self.environ)
        if relation is None:
            raise StatusNotDelivered(f"{app} supply-status relation is not configured")
        if app not in self._tokens:
            self._tokens[app] = ServiceTokenProvider(
                supply_settings(get_settings(), relation)
            )
        token = await self._tokens[app].get_token()
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds, transport=self.transport
        ) as client:
            response = await client.post(
                relation["internal_url"].rstrip("/") + RECEIVER_PATH,
                json=body,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
            )
        if response.status_code >= 300:
            raise StatusNotDelivered(f"{app} answered {response.status_code}")
