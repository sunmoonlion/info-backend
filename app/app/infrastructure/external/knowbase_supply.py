"""调 knowbase 的供给接口（关系 `user-data-supply`，SDD 0043、0047）。

这条关系有自己的服务身份（10-29 Fable 回执第 2 条：部署里一个服务应用只有一个 scope，
knowbase 的接收端绑定从调用方的 service_clients 推出来，不能借入库那份身份）：
`KNOWBASE_INTERNAL_URL` 加 `KNOWBASE_USER_DATA_SUPPLY_SERVICE_{CLIENT_ID,CLIENT_SECRET,
SCOPE,DISCOVERY_URL,BACKCHANNEL_ENDPOINT}`，读法同 knowbench、knowlab 的
`service_relation("knowbase", "user-data-supply")`；缺一项就是没开。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.domain.supply.models import SupplyError
from app.infrastructure.external.knowbase_app import (
    KnowbaseAppNotConfiguredError,
    ServiceTokenProvider,
)
from core.config import Settings, get_settings

SUPPLY_SCOPE = "knowbase:user-data-supply"
SUPPLY_PATH = "/api/internal/v2/user-data"


@dataclass
class HttpKnowbase:
    base_url: str
    tokens: ServiceTokenProvider
    timeout_seconds: float
    transport: httpx.AsyncBaseTransport | None = None  # 测试换成记录请求的

    async def check_confirmation(
        self, confirmation_id: str, subject: str
    ) -> dict[str, Any]:
        return await self._call(
            "GET",
            f"/service/confirmations/{confirmation_id}",
            params={"subject": subject},
        )

    async def deliver_public(
        self,
        *,
        channel: str,
        channel_doc_id: str,
        name: str,
        content: bytes,
        subject: str,
        doc_type: str,
        period_start: date | None,
        period_end: date | None,
        edition: str,
        topics: tuple[str, ...],
        disclosed_on: str | None,
        source_url: str,
    ) -> dict[str, Any]:
        params: list[tuple[str, str]] = [
            ("channel", channel),
            ("channel_doc_id", channel_doc_id),
            ("name", name),
            ("subject", subject),
            ("doc_type", doc_type),
            ("edition", edition),
            ("source_url", source_url),
        ]
        params += [("topics", t) for t in topics]
        for field, value in (
            ("period_start", period_start),
            ("period_end", period_end),
        ):
            if value is not None:
                params.append((field, value.isoformat()))
        if disclosed_on:
            params.append(("disclosed_on", disclosed_on))
        return await self._call(
            "POST", "/service/public-originals", params=params, content=content
        )

    async def add_references(
        self, material_id: str, version_no: int, references: list[tuple[str, str]]
    ) -> int:
        body = await self._call(
            "POST",
            f"/service/public-originals/{material_id}/references",
            json_body={
                "version_no": version_no,
                "references": [{"subject": s, "ref": r} for s, r in references],
            },
        )
        return int(body["recorded"])

    async def processing_grant(self, material_id: str, version_no: int) -> str:
        body = await self._call(
            "POST",
            "/processing/grants",
            json_body={
                "owner": "public:disclosure",
                "material_id": material_id,
                "version_no": version_no,
            },
        )
        return str(body["grant_id"])

    async def write_pages(
        self, grant_id: str, processor_version: str, pages: list[dict[str, object]]
    ) -> dict[str, Any]:
        return await self._call(
            "POST",
            f"/processing/grants/{grant_id}/renditions/pages",
            params={"processor_version": processor_version},
            content=json.dumps(pages, ensure_ascii=False).encode("utf-8"),
        )

    async def write_failed(
        self, grant_id: str, kind: str, processor_version: str, reason: str
    ) -> dict[str, Any]:
        return await self._call(
            "POST",
            f"/processing/grants/{grant_id}/renditions/{kind}",
            params={"processor_version": processor_version, "failed_reason": reason},
        )

    async def write_facts(
        self,
        grant_id: str,
        processor_version: str,
        raw: list[dict[str, object]],
        standard: list[dict[str, object]],
    ) -> dict[str, Any]:
        return await self._call(
            "POST",
            f"/processing/grants/{grant_id}/facts",
            json_body={
                "processor_version": processor_version,
                "raw": raw,
                "standard": standard,
            },
        )

    async def _call(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | list[tuple[str, str]] | None = None,
        json_body: dict[str, Any] | None = None,
        content: bytes | None = None,
    ) -> dict[str, Any]:
        token = await self.tokens.get_token()
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds, transport=self.transport
        ) as client:
            response = await client.request(
                method,
                self.base_url.rstrip("/") + path,
                params=params,
                json=json_body,
                content=content,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accept": "application/json",
                },
            )
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code >= 400:
            code = body.get("code") if isinstance(body, dict) else None
            # knowbase 说「不对」的照实转；它暂时不可用的标成可重试
            status = response.status_code if response.status_code < 500 else 503
            raise SupplyError(status, str(code or "knowbase_error"), "knowbase refused")
        return body if isinstance(body, dict) else {}


def supply_relation(environ: Mapping[str, str] | None = None) -> dict[str, str] | None:
    """这条关系配全了才返回；缺任何一项、scope 不对、内部地址不是 https 就是没开。"""
    env = os.environ if environ is None else environ
    prefix = "KNOWBASE_USER_DATA_SUPPLY_SERVICE_"
    values = {
        "internal_url": env.get("KNOWBASE_INTERNAL_URL", "").strip(),
        "client_id": env.get(prefix + "CLIENT_ID", "").strip(),
        "client_secret": env.get(prefix + "CLIENT_SECRET", "").strip(),
        "scope": env.get(prefix + "SCOPE", "").strip(),
        "discovery_url": env.get(prefix + "DISCOVERY_URL", "").strip(),
        "backchannel_endpoint": env.get(prefix + "BACKCHANNEL_ENDPOINT", "").strip(),
    }
    if not all(values.values()) or values["scope"] != SUPPLY_SCOPE:
        return None
    if urlsplit(values["internal_url"]).scheme != "https":
        return None
    return values


def supply_settings(settings: Settings, relation: dict[str, str]) -> Settings:
    """令牌提供者读的是 knowbase_app_service_* 这组：换成这条关系自己的身份。"""
    return settings.model_copy(
        update={
            "knowbase_app_service_client_id": relation["client_id"],
            "knowbase_app_service_client_secret": relation["client_secret"],
            "knowbase_app_service_scope": relation["scope"],
            "knowbase_app_service_discovery_url": relation["discovery_url"],
            "knowbase_app_service_backchannel_endpoint": relation[
                "backchannel_endpoint"
            ],
        }
    )


@lru_cache(maxsize=1)
def get_knowbase_supply() -> HttpKnowbase:
    settings = get_settings()
    relation = supply_relation()
    if relation is None:
        raise KnowbaseAppNotConfiguredError(
            "knowbase user-data-supply relation is not configured"
        )
    return HttpKnowbase(
        base_url=relation["internal_url"].rstrip("/") + SUPPLY_PATH,
        tokens=ServiceTokenProvider(supply_settings(settings, relation)),
        timeout_seconds=settings.knowbase_supply_timeout_seconds,
    )
