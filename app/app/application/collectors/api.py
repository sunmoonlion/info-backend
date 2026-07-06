from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime

import httpx

from app.application.collectors.base import CollectedLink
from core.config import get_settings


class ApiCollectorAdapter:
    collector_type = "api"

    async def discover(self, *, url: str, config: dict) -> list[CollectedLink]:
        settings = get_settings()
        timeout = float(config.get("timeout_seconds", settings.crawl_timeout_seconds))
        headers = dict(config.get("headers") or {})
        headers.setdefault("User-Agent", settings.crawl_user_agent)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(url, headers=headers, params=config.get("params"))
            response.raise_for_status()
        return parse_api_payload(response.json(), config=config)


def parse_api_payload(payload: object, *, config: dict) -> list[CollectedLink]:
    items = _select_items(payload, str(config.get("items_path", "")))
    url_field = str(config.get("url_field", "url"))
    title_field = str(config.get("title_field", "title"))
    published_at_field = str(config.get("published_at_field", "published_at"))
    summary_field = str(config.get("summary_field", "summary"))

    links: list[CollectedLink] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        url = _string(item.get(url_field))
        if not url:
            continue
        links.append(
            CollectedLink(
                url=url,
                title=_string(item.get(title_field)),
                published_at=_parse_iso_datetime(_string(item.get(published_at_field))),
                summary=_string(item.get(summary_field)),
                metadata={
                    "api_id": _string(item.get(config.get("id_field", "id"))) or "",
                },
            )
        )
    return links


def _select_items(payload: object, path: str) -> Iterable[object]:
    node = payload
    if path:
        for part in path.split("."):
            if not part:
                continue
            if isinstance(node, dict):
                node = node.get(part)
            else:
                return []
    if isinstance(node, list):
        return node
    if isinstance(node, dict):
        return [node]
    return []


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _string(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
