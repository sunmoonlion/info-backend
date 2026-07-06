from __future__ import annotations

from app.application.collectors.base import CollectedLink


class PlaywrightCollectorAdapter:
    collector_type = "playwright"

    async def discover(self, *, url: str, config: dict) -> list[CollectedLink]:
        if not config.get("enabled", False):
            raise ValueError("playwright collector must be explicitly enabled")
        raise NotImplementedError(
            "Playwright collection is reserved for high-value dynamic sources"
        )
