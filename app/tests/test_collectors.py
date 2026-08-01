from datetime import UTC, datetime

import pytest

from app.application.collectors import get_collector_adapter
from app.application.collectors.api import parse_api_payload
from app.application.collectors.changedetection import ChangeDetectionCollectorAdapter
from app.application.collectors.playwright import PlaywrightCollectorAdapter
from app.application.collectors.rss import parse_feed
from app.application.collectors.scrapy import ScrapyCollectorAdapter


def test_parse_rss_feed() -> None:
    links = parse_feed(
        """<?xml version="1.0"?>
        <rss version="2.0">
          <channel>
            <item>
              <title>Market Update</title>
              <link>https://example.com/news/1</link>
              <pubDate>Mon, 06 Jul 2026 09:00:00 GMT</pubDate>
              <description>Summary</description>
              <guid>abc</guid>
            </item>
          </channel>
        </rss>
        """
    )

    assert len(links) == 1
    assert links[0].url == "https://example.com/news/1"
    assert links[0].title == "Market Update"
    assert links[0].published_at == datetime(2026, 7, 6, 9, tzinfo=UTC)
    assert links[0].metadata["guid"] == "abc"


def test_parse_atom_feed() -> None:
    links = parse_feed(
        """<?xml version="1.0"?>
        <feed xmlns="http://www.w3.org/2005/Atom">
          <entry>
            <id>tag:example.com,2026:item</id>
            <title>Policy Note</title>
            <updated>2026-07-06T10:30:00Z</updated>
            <link href="https://example.com/policy" rel="alternate" />
            <summary>Atom summary</summary>
          </entry>
        </feed>
        """
    )

    assert len(links) == 1
    assert links[0].url == "https://example.com/policy"
    assert links[0].title == "Policy Note"
    assert links[0].published_at == datetime(2026, 7, 6, 10, 30, tzinfo=UTC)
    assert links[0].metadata["id"] == "tag:example.com,2026:item"


def test_collector_registry_rejects_unknown_type() -> None:
    with pytest.raises(ValueError, match="unsupported collector type"):
        get_collector_adapter("unknown")


def test_scrapy_adapter_is_explicit_placeholder() -> None:
    adapter = get_collector_adapter("scrapy")
    assert adapter.collector_type == "scrapy"


def test_parse_api_payload_with_items_path() -> None:
    links = parse_api_payload(
        {
            "data": {
                "items": [
                    {
                        "id": 7,
                        "url": "https://example.com/api-news",
                        "title": "API News",
                        "published_at": "2026-07-06T12:00:00Z",
                    }
                ]
            }
        },
        config={"items_path": "data.items"},
    )

    assert len(links) == 1
    assert links[0].url == "https://example.com/api-news"
    assert links[0].metadata["api_id"] == "7"


@pytest.mark.asyncio
async def test_changedetection_adapter_creates_single_trigger_link() -> None:
    adapter = ChangeDetectionCollectorAdapter()
    links = await adapter.discover(
        url="https://example.com/watch",
        config={"watch_id": "watch-1", "title": "Watch"},
    )

    assert len(links) == 1
    assert links[0].url == "https://example.com/watch"
    assert links[0].metadata["watch_id"] == "watch-1"


@pytest.mark.asyncio
async def test_scrapy_adapter_imports_external_worker_results() -> None:
    adapter = ScrapyCollectorAdapter()

    links = await adapter.discover(
        url="https://example.com/seed",
        config={
            "spider_name": "example_news",
            "results": [
                {
                    "url": "https://example.com/news/2",
                    "title": "Scraped News",
                    "published_at": "2026-07-07T08:00:00Z",
                    "metadata": {"depth": 2},
                }
            ],
        },
    )

    assert len(links) == 1
    assert links[0].url == "https://example.com/news/2"
    assert links[0].title == "Scraped News"
    assert links[0].published_at == datetime(2026, 7, 7, 8, tzinfo=UTC)
    assert links[0].metadata["collector_source"] == "scrapy"
    assert links[0].metadata["spider_name"] == "example_news"
    assert links[0].metadata["depth"] == "2"


@pytest.mark.asyncio
async def test_playwright_adapter_imports_rendered_worker_results() -> None:
    adapter = PlaywrightCollectorAdapter()

    links = await adapter.discover(
        url="https://example.com/dynamic",
        config={
            "enabled": True,
            "links": ["https://example.com/dynamic/article"],
        },
    )

    assert len(links) == 1
    assert links[0].url == "https://example.com/dynamic/article"
    assert links[0].metadata["collector_source"] == "playwright"
    assert links[0].metadata["rendered_url"] == "https://example.com/dynamic"


@pytest.mark.asyncio
async def test_external_crawler_adapters_require_results() -> None:
    scrapy_adapter = ScrapyCollectorAdapter()
    playwright_adapter = PlaywrightCollectorAdapter()

    with pytest.raises(ValueError, match="config.results"):
        await scrapy_adapter.discover(
            url="https://example.com/seed",
            config={"spider_name": "example_news"},
        )
    with pytest.raises(ValueError, match="config.results"):
        await playwright_adapter.discover(
            url="https://example.com/dynamic",
            config={"enabled": True},
        )
