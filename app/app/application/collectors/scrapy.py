from __future__ import annotations

from app.application.collectors.base import CollectedLink
from app.application.collectors.external_results import parse_external_links


class ScrapyCollectorAdapter:
    collector_type = "scrapy"

    async def discover(self, *, url: str, config: dict) -> list[CollectedLink]:
        spider_name = config.get("spider_name")
        if not spider_name:
            raise ValueError("scrapy collector requires config.spider_name")
        links = parse_external_links(
            config=config,
            source="scrapy",
            extra_metadata={"spider_name": str(spider_name)},
        )
        if not links:
            raise ValueError(
                "scrapy collector requires config.results from crawler worker"
            )
        return links
