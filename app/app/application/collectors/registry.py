from __future__ import annotations

from app.application.collectors.api import ApiCollectorAdapter
from app.application.collectors.base import CollectorAdapter
from app.application.collectors.changedetection import ChangeDetectionCollectorAdapter
from app.application.collectors.playwright import PlaywrightCollectorAdapter
from app.application.collectors.rss import RssCollectorAdapter
from app.application.collectors.scrapy import ScrapyCollectorAdapter
from app.application.ports.crawl import CrawlFetch


def get_collector_adapter(
    collector_type: str, *, fetch: CrawlFetch
) -> CollectorAdapter:
    """`fetch` 没有默认值：自己发请求的采集器用哪个取数函数，必须由调用方明说。"""
    normalized = collector_type.strip().lower()
    adapters: dict[str, CollectorAdapter] = {
        "rss": RssCollectorAdapter(fetch),
        "atom": RssCollectorAdapter(fetch),
        "api": ApiCollectorAdapter(fetch),
        "changedetection": ChangeDetectionCollectorAdapter(),
        "scrapy": ScrapyCollectorAdapter(),
        "playwright": PlaywrightCollectorAdapter(),
    }
    try:
        return adapters[normalized]
    except KeyError as exc:
        raise ValueError(f"unsupported collector type: {collector_type}") from exc
