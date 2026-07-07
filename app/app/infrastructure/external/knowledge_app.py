from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from core.config import get_settings


class KnowledgeAppNotConfiguredError(RuntimeError):
    pass


@dataclass(frozen=True)
class KnowledgeAppClient:
    ingest_url: str | None
    api_key: str | None
    timeout_seconds: float

    @property
    def enabled(self) -> bool:
        return bool(self.ingest_url)

    async def ingest_document(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self.ingest_url:
            raise KnowledgeAppNotConfiguredError("KNOWLEDGE_APP_INGEST_URL is not configured")

        headers: dict[str, str] = {}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
            response = await client.post(self.ingest_url, json=payload, headers=headers)
            response.raise_for_status()
            try:
                body: Any = response.json()
            except ValueError:
                body = response.text
            return {
                "status_code": response.status_code,
                "body": body,
            }


def get_knowledge_app_client() -> KnowledgeAppClient:
    settings = get_settings()
    return KnowledgeAppClient(
        ingest_url=settings.knowledge_app_ingest_url,
        api_key=settings.knowledge_app_api_key,
        timeout_seconds=settings.knowledge_app_timeout_seconds,
    )
