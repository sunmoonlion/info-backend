from __future__ import annotations

from app.application.collectors.base import CollectedLink


class ScrapyCollectorAdapter:
    collector_type = "scrapy"

    async def discover(self, *, url: str, config: dict) -> list[CollectedLink]:
        spider_name = config.get("spider_name")
        if not spider_name:
            raise ValueError("scrapy collector requires config.spider_name")
        raise NotImplementedError(
            "Scrapy execution is reserved for the dedicated crawler worker phase"
        )
