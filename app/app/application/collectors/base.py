from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class CollectedLink:
    url: str
    title: str | None = None
    published_at: datetime | None = None
    summary: str | None = None
    metadata: dict[str, str] = field(default_factory=dict)


class CollectorAdapter(Protocol):
    collector_type: str

    async def discover(self, *, url: str, config: dict) -> list[CollectedLink]:
        """Discover URLs to be turned into crawl jobs."""
        ...
