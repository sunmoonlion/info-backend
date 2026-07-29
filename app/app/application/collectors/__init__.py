from app.application.collectors.api import ApiCollectorAdapter
from app.application.collectors.base import CollectedLink, CollectorAdapter
from app.application.collectors.changedetection import ChangeDetectionCollectorAdapter
from app.application.collectors.playwright import PlaywrightCollectorAdapter
from app.application.collectors.registry import get_collector_adapter
from app.application.collectors.rss import RssCollectorAdapter
from app.application.collectors.scrapy import ScrapyCollectorAdapter

__all__ = [
    "CollectedLink",
    "CollectorAdapter",
    "RssCollectorAdapter",
    "ApiCollectorAdapter",
    "ChangeDetectionCollectorAdapter",
    "ScrapyCollectorAdapter",
    "PlaywrightCollectorAdapter",
    "get_collector_adapter",
]
