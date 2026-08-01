from __future__ import annotations

from app.application.collectors.api import ApiCollectorAdapter
from app.application.collectors.base import CollectorAdapter
from app.application.collectors.changedetection import ChangeDetectionCollectorAdapter
from app.application.collectors.playwright import PlaywrightCollectorAdapter
from app.application.collectors.rss import RssCollectorAdapter
from app.application.collectors.scrapy import ScrapyCollectorAdapter


def get_collector_adapter(collector_type: str) -> CollectorAdapter:
    normalized = collector_type.strip().lower()
    adapters: dict[str, CollectorAdapter] = {
        "rss": RssCollectorAdapter(),
        "atom": RssCollectorAdapter(),
        "api": ApiCollectorAdapter(),
        "changedetection": ChangeDetectionCollectorAdapter(),
        "scrapy": ScrapyCollectorAdapter(),
        "playwright": PlaywrightCollectorAdapter(),
    }
    try:
        return adapters[normalized]
    except KeyError as exc:
        raise ValueError(f"unsupported collector type: {collector_type}") from exc
