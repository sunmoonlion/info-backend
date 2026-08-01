from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime

from app.application.collectors.base import CollectedLink


def parse_external_links(
    *,
    config: dict,
    source: str,
    extra_metadata: dict[str, str] | None = None,
) -> list[CollectedLink]:
    """Parse link results produced by an external crawler worker."""
    items = _select_items(config)
    url_field = str(config.get("url_field", "url"))
    title_field = str(config.get("title_field", "title"))
    published_at_field = str(config.get("published_at_field", "published_at"))
    summary_field = str(config.get("summary_field", "summary"))
    metadata_field = str(config.get("metadata_field", "metadata"))

    links: list[CollectedLink] = []
    for item in items:
        if isinstance(item, str):
            links.append(
                CollectedLink(
                    url=item,
                    metadata=_metadata(source=source, extra=extra_metadata),
                )
            )
            continue
        if not isinstance(item, Mapping):
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
                metadata=_metadata(
                    source=source,
                    item_metadata=item.get(metadata_field),
                    extra=extra_metadata,
                ),
            )
        )
    return links


def _select_items(config: dict) -> Iterable[object]:
    value = (
        config.get("results")
        or config.get("items")
        or config.get("links")
        or config.get("urls")
    )
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _metadata(
    *,
    source: str,
    item_metadata: object = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    metadata = {"collector_source": source}
    if isinstance(item_metadata, Mapping):
        for key, value in item_metadata.items():
            text = _string(value)
            if text is not None:
                metadata[str(key)] = text
    if extra:
        metadata.update(extra)
    return metadata


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
