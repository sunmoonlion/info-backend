"""调 knowbase 的供给接口（关系 `user-data-supply`，SDD 0043、0047）。

这条关系有自己的服务身份（10-29 Fable 回执第 2 条：部署里一个服务应用只有一个 scope，
knowbase 的接收端绑定从调用方的 service_clients 推出来，不能借入库那份身份）：
`KNOWBASE_INTERNAL_URL` 加 `KNOWBASE_USER_DATA_SUPPLY_SERVICE_{CLIENT_ID,CLIENT_SECRET,
SCOPE,DISCOVERY_URL,BACKCHANNEL_ENDPOINT}`，读法同 knowbench、knowlab 的
`service_relation("knowbase", "user-data-supply")`；缺一项就是没开。

凭加工授权读原件走另一条关系 `user-data-read`（knowbase 的读接口只认这个 scope；一个服务
应用只有一个 scope，所以另有一份身份）：`KNOWBASE_USER_DATA_READ_SERVICE_{CLIENT_ID,
CLIENT_SECRET,SCOPE,DISCOVERY_URL,BACKCHANNEL_ENDPOINT}`，scope `knowbase:user-data-read`
（`HttpKnowbaseReader`，上传资料的加工用）。
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from functools import lru_cache
from typing import Any
from urllib.parse import quote, urlsplit

import httpx

from app.domain.supply.models import REASON_AWAITING_KNOWBASE, SupplyError
from app.infrastructure.external.knowbase_app import (
    KnowbaseAppNotConfiguredError,
    ServiceTokenProvider,
)
from core.config import Settings, get_settings

SUPPLY_SCOPE = "knowbase:user-data-supply"
READ_SCOPE = "knowbase:user-data-read"
SUPPLY_PATH = "/api/internal/v2/user-data"
PUBLIC_OWNER = "public:disclosure"


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

    async def deliver_collected(
        self,
        *,
        channel: str,
        channel_doc_id: str,
        name: str,
        content: bytes,
        license: str,
        source_url: str,
        beneficiaries: list[dict[str, object]],
        default_description: dict[str, object] | None = None,
    ) -> list[dict[str, Any]]:
        """为用户采集的一份原件（0045 第 3.5 节「非公开的内容只存一份、每个用户各得一份
        自己名下的资料」）：`POST /service/collected-originals`，查询参数写渠道、渠道内编号、
        渠道审定的许可档、出处、文件名；multipart：`file` 是原件字节，`beneficiaries`
        是 JSON `[{subject, prefill, item_ref, basis}]`（预填的描述只用在这个人自己那份），
        `default_description` 是渠道条目给的未确认候选描述（`confirmed: false`）。回
        `[{subject, material_id, version_no, created, has_readings, readings}]`。knowbase
        这条路没上（404、405）抛 409 `awaiting_knowbase`，调用方不交付，绝不改走公开原件层。"""
        data: dict[str, str] = {
            "beneficiaries": json.dumps(beneficiaries, ensure_ascii=False)
        }
        if default_description is not None:
            data["default_description"] = json.dumps(
                default_description, ensure_ascii=False
            )
        body = await self._send(
            "POST",
            "/service/collected-originals",
            params=[
                ("channel", channel),
                ("channel_doc_id", channel_doc_id),
                ("name", name),
                ("license", license),
                ("source_url", source_url),
            ],
            files={"file": (name, content, "application/octet-stream")},
            data=data,
        )
        found = body if isinstance(body, list) else []
        return [dict(b) for b in found if isinstance(b, dict)]

    async def check_subscription(
        self, subscription_id: str, revision: int, subject: str
    ) -> dict[str, Any] | None:
        """订阅范围同意还在不在（knowbase `GET /service/subscriptions/{订阅}/revisions/{修订}`，
        关系 user-data-supply）：撤了回 None；因到期失效回 `{"status": "expired"}`（三审第 10 条）。"""
        try:
            return await self._call(
                "GET",
                f"/service/subscriptions/{quote(subscription_id, safe='')}"
                f"/revisions/{int(revision)}",
                params={"subject": subject},
            )
        except SupplyError as exc:
            if exc.code in ("subscription_expired", "consent_expired"):
                return {"status": "expired", "code": exc.code}
            if exc.status == 404:
                return None
            raise

    async def deliver_public(
        self,
        *,
        channel: str,
        channel_doc_id: str,
        name: str,
        content: bytes,
        subject: str | None,
        doc_type: str,
        period_start: date | None,
        period_end: date | None,
        edition: str,
        topics: tuple[str, ...],
        disclosed_on: str | None,
        source_url: str,
    ) -> dict[str, Any]:
        """交一份法定披露（审核定为 `disclosure` 区的渠道取来的）：进公开原件层，许可由
        knowbase 定为「可以经专家给别人用」。描述按渠道给的，不带任何用户的预填。"""
        params: list[tuple[str, str]] = [
            ("channel", channel),
            ("channel_doc_id", channel_doc_id),
            ("name", name),
            ("doc_type", doc_type),
            ("edition", edition),
            ("source_url", source_url),
        ]
        if subject:
            params.append(("subject", subject))
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
        payload_refs: list[dict[str, object]] = []
        for item in references:
            s, r = item
            ref_dict: dict[str, object] = {"subject": s, "ref": r}
            basis = getattr(item, "basis", None)
            if isinstance(basis, dict) and basis:
                ref_dict["basis"] = basis
            payload_refs.append(ref_dict)
        body = await self._call(
            "POST",
            f"/service/public-originals/{material_id}/references",
            json_body={
                "version_no": version_no,
                "references": payload_refs,
            },
        )
        return int(body["recorded"])

    async def processing_grant(
        self, material_id: str, version_no: int, *, owner: str = PUBLIC_OWNER
    ) -> str:
        body = await self._call(
            "POST",
            "/processing/grants",
            json_body={
                "owner": owner,
                "material_id": material_id,
                "version_no": version_no,
            },
        )
        return str(body["grant_id"])

    async def pending_processing(self, limit: int) -> list[tuple[str, str, int]]:
        body = await self._call_list(
            "GET", "/processing/pending", params={"limit": str(limit)}
        )
        return [
            (str(i["owner"]), str(i["material_id"]), int(i["version_no"]))
            for i in body
            if isinstance(i, dict)
        ]

    async def write_text(
        self,
        grant_id: str,
        processor_version: str,
        body: str,
        *,
        title: str | None,
        published_on: date | None,
    ) -> dict[str, Any]:
        """正文：请求体是 UTF-8 文字，可带标题、发布日期（knowbase 契约「读法写回格式」）。"""
        params = {"processor_version": processor_version}
        if title:
            params["title"] = title[:500]
        if published_on is not None:
            params["published_on"] = published_on.isoformat()
        return await self._call(
            "POST",
            f"/processing/grants/{grant_id}/renditions/text",
            params=params,
            content=body.encode("utf-8"),
        )

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
        body = await self._send(
            method, path, params=params, json_body=json_body, content=content
        )
        return body if isinstance(body, dict) else {}

    async def _call_list(
        self, method: str, path: str, *, params: dict[str, str] | None = None
    ) -> list[Any]:
        body = await self._send(method, path, params=params)
        return body if isinstance(body, list) else []

    async def _send(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | list[tuple[str, str]] | None = None,
        json_body: dict[str, Any] | None = None,
        content: bytes | None = None,
        files: dict[str, Any] | None = None,
        data: dict[str, str] | None = None,
    ) -> Any:
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
                files=files,
                data=data,
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
            if files is not None and response.status_code in (404, 405) and not code:
                # knowbase 还没有「为某用户采集」这条路（路由不在）：不交付，也不重试
                raise SupplyError(
                    409, REASON_AWAITING_KNOWBASE, "knowbase has no collected originals"
                )
            # knowbase 说「不对」的照实转；它暂时不可用的标成可重试
            status = response.status_code if response.status_code < 500 else 503
            raise SupplyError(status, str(code or "knowbase_error"), "knowbase refused")
        return body


@dataclass
class HttpKnowbaseReader:
    """凭加工授权读原件、用完交还（关系 `user-data-read`）。"""

    base_url: str
    tokens: ServiceTokenProvider
    timeout_seconds: float
    transport: httpx.AsyncBaseTransport | None = None

    async def content(
        self, grant_id: str, material_id: str, version_no: int
    ) -> tuple[bytes, str]:
        response = await self._request(
            "GET",
            f"/run-grants/{grant_id}/materials/{material_id}/content",
            params={"version_no": str(version_no)},
        )
        media_type = response.headers.get("content-type", "application/octet-stream")
        return response.content, media_type

    async def release(self, grant_id: str) -> None:
        await self._request("POST", f"/run-grants/{grant_id}/release")

    async def _request(
        self, method: str, path: str, *, params: dict[str, str] | None = None
    ) -> httpx.Response:
        token = await self.tokens.get_token()
        async with httpx.AsyncClient(
            timeout=self.timeout_seconds, transport=self.transport
        ) as client:
            response = await client.request(
                method,
                self.base_url.rstrip("/") + path,
                params=params,
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = {}
            code = body.get("code") if isinstance(body, dict) else None
            status = response.status_code if response.status_code < 500 else 503
            raise SupplyError(status, str(code or "knowbase_error"), "knowbase refused")
        return response


def supply_relation(environ: Mapping[str, str] | None = None) -> dict[str, str] | None:
    """这条关系配全了才返回；缺任何一项、scope 不对、内部地址不是 https 就是没开。"""
    return knowbase_relation("user-data-supply", SUPPLY_SCOPE, environ)


def read_relation(environ: Mapping[str, str] | None = None) -> dict[str, str] | None:
    """凭授权读的那条关系（`user-data-read`），读法同上。"""
    return knowbase_relation("user-data-read", READ_SCOPE, environ)


def knowbase_relation(
    relation: str, scope: str, environ: Mapping[str, str] | None = None
) -> dict[str, str] | None:
    env = os.environ if environ is None else environ
    prefix = "KNOWBASE_" + relation.upper().replace("-", "_") + "_SERVICE_"
    values = {
        "internal_url": env.get("KNOWBASE_INTERNAL_URL", "").strip(),
        "client_id": env.get(prefix + "CLIENT_ID", "").strip(),
        "client_secret": env.get(prefix + "CLIENT_SECRET", "").strip(),
        "scope": env.get(prefix + "SCOPE", "").strip(),
        "discovery_url": env.get(prefix + "DISCOVERY_URL", "").strip(),
        "backchannel_endpoint": env.get(prefix + "BACKCHANNEL_ENDPOINT", "").strip(),
    }
    if not all(values.values()) or values["scope"] != scope:
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


@lru_cache(maxsize=1)
def get_knowbase_reader() -> HttpKnowbaseReader:
    settings = get_settings()
    relation = read_relation()
    if relation is None:
        raise KnowbaseAppNotConfiguredError(
            "knowbase user-data-read relation is not configured"
        )
    return HttpKnowbaseReader(
        base_url=relation["internal_url"].rstrip("/") + SUPPLY_PATH,
        tokens=ServiceTokenProvider(supply_settings(settings, relation)),
        timeout_seconds=settings.knowbase_supply_timeout_seconds,
    )
