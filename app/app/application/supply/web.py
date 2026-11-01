"""通用网页型、订阅源型、搜索型适配器（SDD 0045 第 3.2 节，第七节块 3；发版前补齐 F4）。

三类都按网站（域名）登记一条渠道，经框架给的 `PageHttp`（只连公网、限大小与重定向、经这条
渠道的限速）取，插件不自带网络客户端。

- **通用网页型**（`web`）：只会「取」。网址要落在渠道登记的网站上（按网站审，0045 第 3.1 节）；
  守 robots（渠道登记默认守，审核可逐渠道改）；审核定了「渲染」的（`render_allowed` 且配置
  `render: true`）交渲染工作负载，平台没起渲染服务就不渲染、照实回 `render_unavailable`。
  「往下一层跟哪些文件链接」：同一网站、只往下一层、按配置的后缀（默认 PDF、Word、Excel、
  CSV、文本），最多 20 份，超出的回给用户「还有几个没取」（0045 第 2.2 节）。
- **订阅源型**（`feed`）：会「看新的」与「取」。RSS、Atom 按条目；栏目页（不是订阅源格式的
  网页）按页面里同一网站的链接，配置 `link_pattern` 可只认列表位置的那些链接（网页变化
  监测）。游标记见过的条目，订同一个对象的人共用。
- **搜索型**（`search`）：只会「搜」。第一版只做审过的站内搜索（所有者 10-26 定：通用搜索
  接口等律师意见）：配置 `search_url`（带 `{q}`），结果是页面里同一网站的链接，`result_pattern`
  可只认结果位置的链接；按页面给的原始顺序，不重排、不评级。

取回来的一律原样留存（`jobs.py`），这里不改内容。
"""

from __future__ import annotations

import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import quote, urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from app.application.ports.supply import Page, PageHttp, Renderer
from app.domain.supply.catalog import normalize_url, on_site, site_of, web_doc_id
from app.domain.supply.models import (
    MAX_LINKED,
    REASON_OFF_SITE,
    REASON_RENDER_UNAVAILABLE,
    REASON_ROBOTS,
    Channel,
    DescriptionKey,
    Entry,
    SupplyError,
)

PAGE_MAX_BYTES = 10 * 1024 * 1024
FILE_MAX_BYTES = 128 * 1024 * 1024
PAGE_TIMEOUT_SECONDS = 30.0
FILE_TIMEOUT_SECONDS = 180.0
ROBOTS_MAX_BYTES = 512 * 1024
ROBOTS_TTL_SECONDS = 24 * 3600
SEEN_KEPT = 1000  # 游标里记最近见过的多少条
DEFAULT_FOLLOW = (".pdf", ".doc", ".docx", ".xls", ".xlsx", ".csv", ".txt")
_HTML = ("text/html", "application/xhtml+xml")
_FEED = ("application/rss+xml", "application/atom+xml", "application/xml", "text/xml")
_ATOM = "{http://www.w3.org/2005/Atom}"

# robots 规则按「协议 + 主机」在进程里留一天：（取到的时间，规则；None 是全都可以取）
_ROBOTS: dict[str, tuple[float, RobotFileParser | None]] = {}


def _base_type(media_type: str) -> str:
    return media_type.split(";", 1)[0].strip().lower()


def is_html(media_type: str) -> bool:
    return _base_type(media_type) in _HTML


class _Links(HTMLParser):
    """页面里的链接，按出现的先后，带链接文字。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() == "a":
            self._href = dict(attrs).get("href")
            self._text = []

    def handle_data(self, data: str) -> None:
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "a" and self._href is not None:
            self.links.append((self._href, " ".join("".join(self._text).split())))
            self._href = None


def links_in(
    html: bytes, base_url: str, domain: str, *, pattern: str | None = None
) -> list[tuple[str, str]]:
    """页面里落在同一网站上的 http(s) 链接（规范化、去重，保持先后）：（网址，链接文字）。"""
    parser = _Links()
    try:
        parser.feed(html.decode("utf-8", errors="replace"))
        parser.close()
    except Exception:  # 坏网页：能认出几个算几个
        pass
    wanted = re.compile(pattern) if pattern else None
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for href, text in parser.links:
        if not href or href.startswith(("#", "javascript:", "mailto:")):
            continue
        absolute = urljoin(base_url, href.strip())
        try:
            if not on_site(site_of(absolute), domain):
                continue
            normalized = normalize_url(absolute)
        except SupplyError:
            continue
        if wanted is not None and not wanted.search(normalized):
            continue
        if normalized in seen or normalized == normalize_url(base_url):
            continue
        seen.add(normalized)
        out.append((normalized, text))
    return out


def file_links(
    html: bytes, base_url: str, domain: str, suffixes: tuple[str, ...]
) -> list[tuple[str, str]]:
    """「往下一层跟哪些文件链接」：同一网站、后缀在单子里的链接。"""
    return [
        (url, text)
        for url, text in links_in(html, base_url, domain)
        if urlsplit(url).path.lower().endswith(suffixes)
    ]


def _date(value: str | None) -> date | None:
    if not value:
        return None
    text = value.strip()
    try:
        return parsedate_to_datetime(text).date()
    except (TypeError, ValueError, IndexError):
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    found = re.search(r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})", text)
    if found:
        try:
            return date(int(found[1]), int(found[2]), int(found[3]))
        except ValueError:
            return None
    return None


def feed_items(content: bytes) -> list[tuple[str, str, date | None]] | None:
    """RSS、Atom 的条目：（网址，标题，发布日期）；不是订阅源格式的回 None。

    Python 自带的 expat 挡实体膨胀（2.4.1 起），内容另有大小上限（取的时候限）。"""
    head = content[:2048].lower()
    if b"<rss" not in head and b"<feed" not in head and b"<rdf" not in head:
        return None
    if b"<!doctype" in head or b"<!entity" in content[:8192].lower():
        return None  # 不认带文档类型声明的：订阅源用不着，挡外部实体
    try:
        root = ET.fromstring(content)
    except ET.ParseError:
        return None
    out: list[tuple[str, str, date | None]] = []
    for item in root.iter():
        tag = item.tag
        if tag == "item" or tag.endswith("}item"):
            link = (item.findtext("link") or "").strip()
            if not link:
                guid = item.find("guid")
                if guid is not None and (guid.text or "").startswith("http"):
                    link = (guid.text or "").strip()
            title = (item.findtext("title") or "").strip()
            when = item.findtext("pubDate") or item.findtext(
                "{http://purl.org/dc/elements/1.1/}date"
            )
            if link:
                out.append((link, title, _date(when)))
        elif tag == f"{_ATOM}entry":
            link = ""
            for node in item.findall(f"{_ATOM}link"):
                if node.get("rel") in (None, "alternate") and node.get("href"):
                    link = str(node.get("href")).strip()
                    break
            title = (item.findtext(f"{_ATOM}title") or "").strip()
            when = item.findtext(f"{_ATOM}published") or item.findtext(
                f"{_ATOM}updated"
            )
            if link:
                out.append((link, title, _date(when)))
    return out


@dataclass
class WebSite:
    """一条按网站登记的渠道（通用网页、订阅源、站内搜索）的适配器。"""

    http: PageHttp
    channel: Channel
    renderer: Renderer | None = None
    user_agent: str = "SunmoonAI KnowfeedBot"
    clock: Callable[[], float] = time.monotonic
    _filenames: dict[str, str] = field(default_factory=dict, repr=False)

    def filename_of(self, url: str) -> str | None:
        return self._filenames.get(url)

    # —— 目录型的两个动作：通用网页没有目录，「列」是空；「取」按条目的网址 ——

    async def list(self, key: DescriptionKey) -> list[Entry]:
        return []

    async def fetch(self, entry: Entry) -> tuple[bytes, str]:
        content, media_type, _ = await self.fetch_url(entry.url)
        return content, media_type

    # —— 取 ——

    def _check_site(self, url: str) -> str:
        if not self.channel.domain or not on_site(site_of(url), self.channel.domain):
            raise SupplyError(422, REASON_OFF_SITE, "the URL is not on this site")
        return normalize_url(url)

    async def fetch_url(
        self, url: str, *, file: bool = False
    ) -> tuple[bytes, str, str]:
        """取一个网址：（内容，内容类型，最后落到的网址）。重定向到别的网站的不认（那个
        网站没审过）。"""
        target = self._check_site(url)
        if self.channel.obey_robots:
            await self._robots_allow(target)
        if self.renders and not file:
            if self.renderer is None or not self.renderer.configured():
                raise SupplyError(
                    422, REASON_RENDER_UNAVAILABLE, "the render service is not set up"
                )
            page = await self.renderer.render(
                target, max_bytes=PAGE_MAX_BYTES, timeout_seconds=PAGE_TIMEOUT_SECONDS
            )
        else:
            page = await self.http.page(
                target,
                max_bytes=FILE_MAX_BYTES if file else PAGE_MAX_BYTES,
                timeout_seconds=FILE_TIMEOUT_SECONDS if file else PAGE_TIMEOUT_SECONDS,
                fetch=True,
                site=self.channel.domain,
            )
        _check_status(page)
        final = page.final_url or target
        self._check_site(final)
        if not page.content:
            raise SupplyError(502, "empty_content", "the page is empty")
        if page.filename:
            self._filenames[final] = page.filename
            self._filenames[target] = page.filename
            self._filenames[url] = page.filename
        return page.content, page.media_type, final

    @property
    def renders(self) -> bool:
        """审核定了能渲染、配置要渲染的才渲染（0045 第 3.1 节「能否用无头浏览器渲染」）。"""
        return self.channel.render_allowed and self.channel.config.get("render") is True

    def follow_suffixes(self) -> tuple[str, ...]:
        given = self.channel.config.get("follow_suffixes")
        if isinstance(given, list) and all(isinstance(s, str) for s in given):
            return tuple(s.lower() for s in given if s.startswith("."))
        return DEFAULT_FOLLOW

    def linked_files(
        self, html: bytes, base_url: str
    ) -> tuple[list[tuple[str, str]], int]:
        """它直接链到的文件：（前 20 份，还有几个没取）。"""
        found = file_links(
            html, base_url, str(self.channel.domain), self.follow_suffixes()
        )
        return found[:MAX_LINKED], max(0, len(found) - MAX_LINKED)

    async def _robots_allow(self, url: str) -> None:
        """RFC 9309：robots 取不到（4xx）当全都可以；对方出错、连不上当全都不行，过会儿再试。"""
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        cached = _ROBOTS.get(origin)
        now = self.clock()
        if cached is None or now - cached[0] > ROBOTS_TTL_SECONDS:
            page = await self.http.page(
                origin + "/robots.txt",
                max_bytes=ROBOTS_MAX_BYTES,
                timeout_seconds=PAGE_TIMEOUT_SECONDS,
                fetch=False,
                site=self.channel.domain,
            )
            if page.status >= 500 or page.status == 429:
                raise SupplyError(503, "robots_unreachable", "robots.txt failed")
            rules: RobotFileParser | None = None
            if 200 <= page.status < 300:
                rules = RobotFileParser()
                rules.parse(page.content.decode("utf-8", errors="replace").splitlines())
            cached = (now, rules)
            _ROBOTS[origin] = cached
        rules = cached[1]
        if rules is not None and not rules.can_fetch(self.user_agent, url):
            raise SupplyError(422, REASON_ROBOTS, "robots.txt disallows this URL")

    # —— 看新的（订阅源型） ——

    def _entry(self, url: str, title: str, disclosed: date | None) -> Entry:
        config = self.channel.config
        doc_type = config.get("doc_type")
        if not isinstance(doc_type, str) or not doc_type:
            doc_type = (
                self.channel.doc_types[0] if self.channel.doc_types else "webpage"
            )
        subject = config.get("subject")
        return Entry(
            channel_id=self.channel.id,
            channel_doc_id=web_doc_id(url),
            title=title[:200] or url,
            url=url,
            disclosed_on=disclosed,
            doc_type=doc_type,
            subject=subject if isinstance(subject, str) and subject else None,
        )

    async def watch(
        self, target: str, cursor: dict[str, object]
    ) -> tuple[list[Entry], dict[str, object]]:
        """游标之后新出的条目与新游标。第一次看（游标空）时，没写日期的条目只记下、标成
        起点（订阅不取它们，免得把栏目页上的旧东西都当新的）。"""
        source = self._check_site(target)
        if self.channel.obey_robots:
            await self._robots_allow(source)
        page = await self.http.page(
            source,
            max_bytes=PAGE_MAX_BYTES,
            timeout_seconds=PAGE_TIMEOUT_SECONDS,
            fetch=False,
            site=self.channel.domain,
        )
        _check_status(page)
        items = feed_items(page.content)
        if items is None:
            if not is_html(page.media_type):
                raise SupplyError(502, "unexpected_response", "not a feed or page")
            pattern = self.channel.config.get("link_pattern")
            found = [
                (url, text, None)
                for url, text in links_in(
                    page.content,
                    page.final_url or source,
                    str(self.channel.domain),
                    pattern=pattern if isinstance(pattern, str) else None,
                )
            ]
        else:
            found = []
            for link, title, when in items:
                try:
                    absolute = normalize_url(urljoin(source, link))
                    if not on_site(site_of(absolute), str(self.channel.domain)):
                        continue
                except SupplyError:
                    continue
                found.append((absolute, title, when))
        seen_list = cursor.get("seen")
        seen = set(seen_list) if isinstance(seen_list, list) else set()
        first = not seen and not cursor.get("checked")
        entries = []
        for url, title, when in found:
            entry = self._entry(url, title, when)
            if entry.channel_doc_id in seen:
                continue
            entries.append(entry)
        ids = [e.channel_doc_id for e in entries] + [
            s
            for s in (seen_list if isinstance(seen_list, list) else [])
            if isinstance(s, str)
        ]
        new_cursor: dict[str, object] = {
            "seen": list(dict.fromkeys(ids))[:SEEN_KEPT],
            "checked": True,
        }
        if first:
            new_cursor["baseline"] = [
                e.channel_doc_id for e in entries if e.disclosed_on is None
            ][:SEEN_KEPT]
        return entries, new_cursor

    # —— 搜（搜索型：站内搜索） ——

    async def search(self, topic: str, wanted: int) -> list[dict[str, object]]:
        template = self.channel.config.get("search_url")
        if not isinstance(template, str) or "{q}" not in template:
            raise SupplyError(422, "search_not_configured", "no search_url with {q}")
        url = self._check_site(template.replace("{q}", quote(topic.strip())))
        if self.channel.obey_robots:
            await self._robots_allow(url)
        page = await self.http.page(
            url,
            max_bytes=PAGE_MAX_BYTES,
            timeout_seconds=PAGE_TIMEOUT_SECONDS,
            fetch=False,
            site=self.channel.domain,
        )
        _check_status(page)
        if not is_html(page.media_type):
            raise SupplyError(502, "unexpected_response", "search page is not HTML")
        pattern = self.channel.config.get("result_pattern")
        found = links_in(
            page.content,
            page.final_url or url,
            str(self.channel.domain),
            pattern=pattern if isinstance(pattern, str) else None,
        )
        return [
            {
                "title": text[:200] or link,
                "url": link,
                "snippet": "",
                "date": None,
                "channel_id": self.channel.id,
            }
            for link, text in found[:wanted]
        ]


def _check_status(page: Page) -> None:
    """对方回的状态：429 限流（等到能取再取），5xx 对方出错（重试、计渠道失败），404、410
    是这一份没有，别的 4xx 是对方拒了（独立审第 3 条：4xx 都算「这一条没有」，不计渠道失败）。"""
    if page.status == 429:
        raise SupplyError(429, "channel_rate_limited", "the site asked to slow down")
    if page.status >= 500:
        raise SupplyError(503, "channel_unavailable", "the site is unavailable")
    if page.status in (404, 410):
        raise SupplyError(404, "page_not_found", "the page is gone")
    if page.status >= 400:
        raise SupplyError(422, "channel_refused", f"the site answered {page.status}")
