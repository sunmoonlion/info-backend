"""调 knowbase 的供给接口（关系 `user-data-supply`，SDD 0043、0047）。

和旧的入库接口共用 knowfeed 的服务客户端身份，只把要的 scope 换成
`knowbase:user-data-supply`；knowbase 按关系逐调用方核。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

import httpx

from app.domain.supply.models import DescriptionKey, SupplyError
from app.infrastructure.external.knowbase_app import (
    KnowbaseAppNotConfiguredError,
    ServiceTokenProvider,
)
from core.config import Settings, get_settings

SUPPLY_SCOPE = "knowbase:user-data-supply"


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
        key: DescriptionKey,
        disclosed_on: str | None,
        source_url: str,
    ) -> dict[str, Any]:
        params = {
            "channel": channel,
            "channel_doc_id": channel_doc_id,
            "name": name,
            "subject": key.subject,
            "doc_type": key.doc_type,
            "source_url": source_url,
        }
        for field, value in (
            ("period_start", key.period_start),
            ("period_end", key.period_end),
        ):
            if value is not None:
                params[field] = value.isoformat()
        if disclosed_on:
            params["disclosed_on"] = disclosed_on
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
        params: dict[str, str] | None = None,
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


@lru_cache(maxsize=1)
def get_knowbase_supply() -> HttpKnowbase:
    settings = get_settings()
    if not settings.knowbase_supply_url:
        raise KnowbaseAppNotConfiguredError("knowbase supply url is not configured")
    return HttpKnowbase(
        base_url=settings.knowbase_supply_url,
        tokens=ServiceTokenProvider(_supply_settings(settings)),
        timeout_seconds=settings.knowbase_supply_timeout_seconds,
    )


def _supply_settings(settings: Settings) -> Settings:
    return settings.model_copy(update={"knowbase_app_service_scope": SUPPLY_SCOPE})
