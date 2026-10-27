"""巨潮资讯网（法定信息披露）作为目录型渠道的插件（SDD 0045 第 3.2 节）。

插件只管映射与解析：描述 → 巨潮的公告分类与查询参数，公告列表 → 条目，条目 → 原件。
取数走框架给的 `Http`（只连公网、限大小与重定向），插件不自带网络客户端。

接口地址与参数参考了 akshare 1.18.97（MIT License）的 stock_zh_a_disclosure_report_cninfo，
标题的过滤规矩沿用旧采集器（`securities/collectors/cninfo.py`）在真实年报上试出来的。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Protocol

from app.domain.supply.models import DescriptionKey, Entry, SupplyError

CHANNEL_ID = "cninfo"
_BASE = "http://www.cninfo.com.cn"
_STATIC = "http://static.cninfo.com.cn"
_PAGE_SIZE = 30
_MAX_PAGES = 5
_ADJUNCT = re.compile(r"^finalpage/\d{4}-\d{2}-\d{2}/\d{6,20}\.PDF$", re.IGNORECASE)
_ORG_ID = re.compile(r"^[0-9A-Za-z]{4,32}$")
_SHANGHAI = timezone(timedelta(hours=8))
MAX_REPORT_BYTES = 128 * 1024 * 1024
REPORT_TIMEOUT_SECONDS = 180.0

# 词表类型 → 巨潮的公告分类、全文标题
_CATEGORIES: dict[str, tuple[str, re.Pattern[str]]] = {
    "annual_report": (
        "category_ndbg_szsh",
        # 「年报报告」是发行人写错的标题，实际就是年度报告全文（旧采集器 2026-09-28 实测）
        re.compile(r"(20\d{2})\s*年\s*(?:年度报告|年报报告)"),
    ),
    "semiannual_report": (
        "category_bndbg_szsh",
        re.compile(r"(20\d{2})\s*年\s*(?:半年度报告|中期报告)"),
    ),
    "q1_report": (
        "category_yjdbg_szsh",
        re.compile(r"(20\d{2})\s*年\s*第一季度报告"),
    ),
    "q3_report": (
        "category_sjdbg_szsh",
        re.compile(r"(20\d{2})\s*年\s*第三季度报告"),
    ),
}
# 摘要、英文版、已取消的不要；H 股年报也挂在这一类下面，那不是 A 股的报告
_NOT_FULL = ("摘要", "英文", "取消", "已取消", "H股", "H 股")
_REVISED = ("修订", "更正", "更新")


class Http(Protocol):
    async def post_form(self, url: str, form: tuple[tuple[str, str], ...]) -> bytes: ...

    async def get(self, url: str) -> bytes: ...

    async def download(
        self, url: str, *, max_bytes: int, timeout_seconds: float
    ) -> tuple[bytes, str]:
        """取一个文件：（内容，内容类型）。"""
        ...


@dataclass
class CninfoCatalog:
    http: Http
    _org_ids: dict[str, str] | None = None

    async def list(self, key: DescriptionKey) -> list[Entry]:
        """这一条描述在巨潮能列出的全文条目（新的在前）；没有就是空。"""
        if key.doc_type not in _CATEGORIES or key.period_end is None:
            return []
        code = key.subject.split(".")[0]
        org_id = await self._org_id(code)
        category, title = _CATEGORIES[key.doc_type]
        year = key.period_end.year
        # 报告期结束后才会披露；更正版可能晚一年多
        start = key.period_end
        end = date(year + 2, 6, 30)
        rows: list[dict] = []
        for page in range(1, _MAX_PAGES + 1):
            payload = _json(
                await self.http.post_form(
                    f"{_BASE}/new/hisAnnouncement/query",
                    (
                        ("pageNum", str(page)),
                        ("pageSize", str(_PAGE_SIZE)),
                        ("column", "szse"),
                        ("tabName", "fulltext"),
                        ("plate", ""),
                        ("stock", f"{code},{org_id}"),
                        ("searchkey", ""),
                        ("secid", ""),
                        ("category", category),
                        ("trade", ""),
                        ("seDate", f"{start.isoformat()}~{end.isoformat()}"),
                        ("sortName", ""),
                        ("sortType", ""),
                        ("isHLtitle", "true"),
                    ),
                ),
                "announcement query",
            )
            if not isinstance(payload, dict):
                raise SupplyError(502, "unexpected_response", "announcement query")
            found = payload.get("announcements") or []
            if not isinstance(found, list):
                raise SupplyError(502, "unexpected_response", "announcement list")
            rows.extend(r for r in found if isinstance(r, dict))
            total = payload.get("totalAnnouncement")
            if not isinstance(total, int) or page * _PAGE_SIZE >= total or not found:
                break
        entries = [e for e in (self._entry(key, r, title, year) for r in rows) if e]
        return sorted(entries, key=lambda e: e.disclosed_on or date.min, reverse=True)

    async def fetch(self, entry: Entry) -> tuple[bytes, str]:
        content, media_type = await self.http.download(
            entry.url,
            max_bytes=MAX_REPORT_BYTES,
            timeout_seconds=REPORT_TIMEOUT_SECONDS,
        )
        if not content.startswith(b"%PDF-"):
            raise SupplyError(502, "not_a_pdf", "the channel did not return a PDF")
        return content, "application/pdf"

    def _entry(
        self, key: DescriptionKey, row: dict, title_re: re.Pattern[str], year: int
    ) -> Entry | None:
        title = re.sub(r"</?em>", "", str(row.get("announcementTitle") or ""))
        if any(word in title for word in _NOT_FULL):
            return None
        matched = title_re.search(title)
        adjunct = str(row.get("adjunctUrl") or "")
        announcement_id = str(row.get("announcementId") or "")
        time_ms = row.get("announcementTime")
        if (
            matched is None
            or int(matched.group(1)) != year
            or not _ADJUNCT.fullmatch(adjunct)
            or not announcement_id.isdecimal()
            or not isinstance(time_ms, int)
        ):
            return None
        disclosed = (
            datetime.fromtimestamp(time_ms / 1000, tz=UTC).astimezone(_SHANGHAI).date()
        )
        return Entry(
            channel_id=CHANNEL_ID,
            # 同一份报告的更正版是同一份资料的新版本：编号按报告，不按公告
            channel_doc_id=f"{key.subject.split('.')[0]}:{key.doc_type}:{year}",
            title=title,
            url=f"{_STATIC}/{adjunct}",
            disclosed_on=disclosed,
            revised=any(word in title for word in _REVISED),
            announcement_id=announcement_id,
        )

    async def _org_id(self, code: str) -> str:
        if self._org_ids is None:
            payload = _json(
                await self.http.get(f"{_BASE}/new/data/szse_stock.json"), "stock list"
            )
            stocks = payload.get("stockList") if isinstance(payload, dict) else None
            if not isinstance(stocks, list):
                raise SupplyError(502, "unexpected_response", "stock list")
            self._org_ids = {
                str(s.get("code")): str(s.get("orgId"))
                for s in stocks
                if isinstance(s, dict) and _ORG_ID.fullmatch(str(s.get("orgId") or ""))
            }
        if code not in self._org_ids:
            raise SupplyError(404, "security_not_found_at_source", "not on cninfo")
        return self._org_ids[code]


def _json(content: bytes, what: str):
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise SupplyError(502, "unexpected_response", what) from None
