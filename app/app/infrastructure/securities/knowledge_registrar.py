"""向知识服务登记数据集：服务间令牌 + 内部登记接口。

错误只留稳定错误码与状态码；对方的响应正文、地址、令牌都不进异常与日志。
"""

from __future__ import annotations

from typing import Protocol

import httpx

from app.domain.securities.registration import DatasetRecord, RegistrationError

_RETRYABLE = frozenset({408, 425, 429, 500, 502, 503, 504})


class TokenSource(Protocol):
    async def get_token(self) -> str: ...


class KnowledgeDatasetRegistrar:
    def __init__(
        self,
        *,
        url: str | None,
        tokens: TokenSource | None,
        timeout_seconds: float,
        source_app: str = "info",
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = url
        self._tokens = tokens
        self._timeout = timeout_seconds
        self._source_app = source_app
        self._transport = transport

    @property
    def configured(self) -> bool:
        return bool(self._url and self._tokens)

    async def register(self, dataset: DatasetRecord) -> None:
        if not self._url or not self._tokens:
            raise RegistrationError("registrar_not_configured", retryable=False)
        try:
            token = await self._tokens.get_token()
        except Exception as exc:
            raise RegistrationError(
                "service_token_unavailable", retryable=True, detail=type(exc).__name__
            ) from None
        payload = {
            "dataset_id": dataset.dataset_id,
            "data_version": dataset.data_version,
            "title": dataset.title,
            "security_code": dataset.security_code,
            "object": f"s3://{dataset.bucket}/{dataset.object_key}",
            "object_version_id": dataset.version_id,
            "sha256": dataset.sha256,
            "size_bytes": dataset.size_bytes,
            "start_date": dataset.start_date,
            "end_date": dataset.end_date,
            "source_app": self._source_app,
            "source_ref": dataset.ingestion_id,
            "quality_passed": True,
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post(
                    self._url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            raise RegistrationError(
                "knowledge_unreachable", retryable=True, detail=type(exc).__name__
            ) from None
        status = response.status_code
        if status in (200, 201):
            self._check(response, dataset)
            return
        codes = {
            401: "knowledge_rejected_identity",
            403: "knowledge_rejected_identity",
            404: "knowledge_registry_disabled",
            409: "knowledge_version_conflict",
            422: "knowledge_refused_registration",
        }
        raise RegistrationError(
            codes.get(status, "knowledge_request_failed"),
            retryable=status in _RETRYABLE,
            detail=f"HTTP {status}",
        )

    @staticmethod
    def _check(response: httpx.Response, dataset: DatasetRecord) -> None:
        """对方答复的必须是我们登记的那个版本，而且是现行版本。"""
        try:
            body = response.json()
        except ValueError:
            body = None
        if (
            not isinstance(body, dict)
            or body.get("data_version") != dataset.data_version
            or body.get("sha256") != dataset.sha256
            or body.get("status") != "active"
        ):
            raise RegistrationError("knowledge_reply_unexpected", retryable=False)
