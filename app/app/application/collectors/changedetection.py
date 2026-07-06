from __future__ import annotations

from app.application.collectors.base import CollectedLink


class ChangeDetectionCollectorAdapter:
    collector_type = "changedetection"

    async def discover(self, *, url: str, config: dict) -> list[CollectedLink]:
        watch_id = config.get("watch_id")
        if not watch_id:
            raise ValueError("changedetection collector requires config.watch_id")
        # changedetection.io only triggers the canonical URL here; version diff
        # governance stays in Info App after the crawl job runs.
        return [
            CollectedLink(
                url=url,
                title=config.get("title"),
                metadata={"watch_id": str(watch_id)},
            )
        ]
