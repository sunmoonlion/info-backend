from __future__ import annotations

from app.application.collectors.base import CollectedLink
from app.application.collectors.external_results import parse_external_links


class PlaywrightCollectorAdapter:
    collector_type = "playwright"

    async def discover(self, *, url: str, config: dict) -> list[CollectedLink]:
        if not config.get("enabled", False):
            raise ValueError("playwright collector must be explicitly enabled")
        links = parse_external_links(
            config=config,
            source="playwright",
            extra_metadata={"rendered_url": url},
        )
        if not links:
            raise ValueError(
                "playwright collector requires config.results from crawler worker"
            )
        return links
