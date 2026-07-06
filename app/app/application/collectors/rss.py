from __future__ import annotations

from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from xml.etree import ElementTree

import httpx

from app.application.collectors.base import CollectedLink
from core.config import get_settings


class RssCollectorAdapter:
    collector_type = "rss"

    async def discover(self, *, url: str, config: dict) -> list[CollectedLink]:
        settings = get_settings()
        timeout = float(config.get("timeout_seconds", settings.crawl_timeout_seconds))
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.get(
                url,
                headers={"User-Agent": settings.crawl_user_agent},
            )
            response.raise_for_status()
        return parse_feed(response.text)


def parse_feed(xml_text: str) -> list[CollectedLink]:
    root = ElementTree.fromstring(xml_text)
    if _strip_ns(root.tag) == "feed":
        return _parse_atom(root)
    return _parse_rss(root)


def _parse_rss(root: ElementTree.Element) -> list[CollectedLink]:
    links: list[CollectedLink] = []
    for item in root.findall(".//item"):
        url = _text(item, "link")
        if not url:
            continue
        links.append(
            CollectedLink(
                url=url,
                title=_text(item, "title"),
                published_at=_parse_date(_text(item, "pubDate")),
                summary=_text(item, "description"),
                metadata={"guid": _text(item, "guid") or ""},
            )
        )
    return links


def _parse_atom(root: ElementTree.Element) -> list[CollectedLink]:
    links: list[CollectedLink] = []
    for entry in _children(root, "entry"):
        url = _atom_link(entry)
        if not url:
            continue
        links.append(
            CollectedLink(
                url=url,
                title=_child_text(entry, "title"),
                published_at=_parse_date(
                    _child_text(entry, "updated") or _child_text(entry, "published")
                ),
                summary=_child_text(entry, "summary"),
                metadata={"id": _child_text(entry, "id") or ""},
            )
        )
    return links


def _text(parent: ElementTree.Element, name: str) -> str | None:
    node = parent.find(name)
    return _clean(node.text) if node is not None else None


def _children(parent: ElementTree.Element, local_name: str) -> list[ElementTree.Element]:
    return [child for child in list(parent) if _strip_ns(child.tag) == local_name]


def _child_text(parent: ElementTree.Element, local_name: str) -> str | None:
    for child in _children(parent, local_name):
        return _clean(child.text)
    return None


def _atom_link(entry: ElementTree.Element) -> str | None:
    fallback = None
    for child in _children(entry, "link"):
        href = child.attrib.get("href")
        if not href:
            continue
        if child.attrib.get("rel") in (None, "", "alternate"):
            return href
        fallback = fallback or href
    return fallback


def _parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _strip_ns(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _clean(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None
