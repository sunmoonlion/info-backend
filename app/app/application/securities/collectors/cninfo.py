"""巨潮资讯网（法定信息披露渠道）：年度报告的公告列表与原文。

接口地址与参数参考了 akshare 1.18.97（MIT License，Copyright (c) 2019-2026 Albert King）
的 stock_zh_a_disclosure_report_cninfo；运行时不依赖 akshare（F-INFO-09）。
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime, timedelta, timezone

from app.application.securities.collectors.base import (
    CollectContext,
    parse_json,
    require,
)
from app.domain.securities import (
    CollectError,
    FetchRequest,
    ItemKind,
    SecurityCode,
    SourceCode,
)

_BASE = "http://www.cninfo.com.cn"
_STATIC = "http://static.cninfo.com.cn"
_ANNUAL = "category_ndbg_szsh"
_PAGE_SIZE = 30
_MAX_PAGES = 10
# 原文地址来自响应内容，必须是披露平台固定的归档路径，不能带出别的主机或目录
_ADJUNCT = re.compile(r"^finalpage/\d{4}-\d{2}-\d{2}/\d{6,20}\.PDF$", re.IGNORECASE)
_ORG_ID = re.compile(r"^[0-9A-Za-z]{4,32}$")
# 「年报报告」是发行人写错的标题，实际就是年度报告全文（601899 的 2024 年年报，2026-09-28 实测）
_TITLE_YEAR = re.compile(r"(20\d{2})\s*年\s*(?:年度报告|年报报告)")
# 「H股」：同时在香港上市的公司把 H 股年报也挂在这一类下面，那不是 A 股的年度报告
_NOT_FULL_REPORT = ("摘要", "英文", "取消", "已取消", "H股", "H 股")
_REVISED = ("修订", "更正", "更新")
_SHANGHAI = timezone(timedelta(hours=8))
DEFAULT_MAX_REPORT_BYTES = 128 * 1024 * 1024
DEFAULT_REPORT_TIMEOUT_SECONDS = 180.0


class CninfoDisclosureCollector:
    source = SourceCode.CNINFO.value

    def __init__(
        self,
        *,
        today: date,
        years: int = 8,
        max_report_bytes: int = DEFAULT_MAX_REPORT_BYTES,
        report_timeout_seconds: float = DEFAULT_REPORT_TIMEOUT_SECONDS,
    ) -> None:
        if years < 1 or years > 30:
            raise ValueError("years must be within 1..30")
        if max_report_bytes <= 0 or report_timeout_seconds <= 0:
            raise ValueError("report limits must be positive")
        self._today = today
        self._years = years
        self._max_report_bytes = max_report_bytes
        self._report_timeout_seconds = report_timeout_seconds

    async def collect(self, code: SecurityCode, ctx: CollectContext) -> None:
        org_id = await self._org_id(code, ctx)
        announcements = await self._annual_announcements(code, org_id, ctx)
        reports = _without_relisted(
            [a for a in (self._report(x) for x in announcements) if a]
        )
        if not reports:
            raise CollectError("no_annual_report_found")
        downloaded = 0
        for report in sorted(reports, key=lambda r: r["announcement_id"]):
            # 一份年报取不到（太大、超时、对方出错）不让整个批次失败：
            # 别的年报与报表数据照常采，这一份记为跳过（2026-09-28 批量实测）
            content = await ctx.fetch_optional(
                FetchRequest(
                    source=SourceCode.CNINFO,
                    kind=ItemKind.REPORT_FILE,
                    url=f"{_STATIC}/{report.pop('adjunct_url')}",
                    name=f"annual-{report['fiscal_year']}-{report['announcement_id']}.pdf",
                    meta=report,
                    max_bytes=self._max_report_bytes,
                    timeout_seconds=self._report_timeout_seconds,
                )
            )
            downloaded += content is not None
        if downloaded == 0:
            raise CollectError("no_annual_report_downloaded")

    async def _org_id(self, code: SecurityCode, ctx: CollectContext) -> str:
        content = await ctx.fetch(
            FetchRequest(
                source=SourceCode.CNINFO,
                kind=ItemKind.STOCK_LIST,
                url=f"{_BASE}/new/data/szse_stock.json",
                name="stock-list.json",
            )
        )
        payload = parse_json(content, what="stock list")
        stocks = payload.get("stockList") if isinstance(payload, dict) else None
        if not isinstance(stocks, list):
            raise CollectError("unexpected_response", "stock list")
        for item in stocks:
            if isinstance(item, dict) and item.get("code") == code.code:
                org_id = item.get("orgId")
                if not isinstance(org_id, str) or not _ORG_ID.fullmatch(org_id):
                    raise CollectError("unexpected_response", "org id")
                return org_id
        raise CollectError("security_not_found_at_source", SourceCode.CNINFO.value)

    async def _annual_announcements(
        self, code: SecurityCode, org_id: str, ctx: CollectContext
    ) -> list[dict]:
        start = date(self._today.year - self._years, 1, 1)
        found: list[dict] = []
        for page in range(1, _MAX_PAGES + 1):
            content = await ctx.fetch(
                FetchRequest(
                    source=SourceCode.CNINFO,
                    kind=ItemKind.ANNOUNCEMENT_QUERY,
                    url=f"{_BASE}/new/hisAnnouncement/query",
                    method="POST",
                    form=(
                        ("pageNum", str(page)),
                        ("pageSize", str(_PAGE_SIZE)),
                        ("column", "szse"),
                        ("tabName", "fulltext"),
                        ("plate", ""),
                        ("stock", f"{code.code},{org_id}"),
                        ("searchkey", ""),
                        ("secid", ""),
                        ("category", _ANNUAL),
                        ("trade", ""),
                        (
                            "seDate",
                            f"{start.isoformat()}~{self._today.isoformat()}",
                        ),
                        ("sortName", ""),
                        ("sortType", ""),
                        ("isHLtitle", "true"),
                    ),
                    name=f"annual-announcements-p{page}.json",
                    meta={"page": page, "category": "年报"},
                )
            )
            payload = parse_json(content, what="announcement query")
            require(isinstance(payload, dict), "announcement query")
            rows = payload.get("announcements") or []
            require(isinstance(rows, list), "announcement list")
            found.extend(r for r in rows if isinstance(r, dict))
            total = payload.get("totalAnnouncement")
            if not isinstance(total, int) or page * _PAGE_SIZE >= total or not rows:
                return found
        raise CollectError("too_many_pages", "announcement query")

    def _report(self, row: dict) -> dict | None:
        """只留年度报告全文；摘要、英文版、已取消的不要。修订版保留并标记。"""
        title = re.sub(r"</?em>", "", str(row.get("announcementTitle") or ""))
        if any(word in title for word in _NOT_FULL_REPORT):
            return None
        matched = _TITLE_YEAR.search(title)
        adjunct = str(row.get("adjunctUrl") or "")
        announcement_id = str(row.get("announcementId") or "")
        time_ms = row.get("announcementTime")
        if (
            matched is None
            or not _ADJUNCT.fullmatch(adjunct)
            or not announcement_id.isdecimal()
            or not isinstance(time_ms, int)
            or str(row.get("secCode") or "") == ""
        ):
            return None
        disclosed = datetime.fromtimestamp(time_ms / 1000, tz=UTC).astimezone(_SHANGHAI)
        size = row.get("adjunctSize")
        return {
            "fiscal_year": int(matched.group(1)),
            "title": title,
            "announcement_id": announcement_id,
            "announcement_time_ms": time_ms,
            "official_disclosed_date": disclosed.date().isoformat(),
            "revised": any(word in title for word in _REVISED),
            "adjunct_url": adjunct,
            "listed_size_kb": size if isinstance(size, int) else None,
        }


def _without_relisted(reports: list[dict]) -> list[dict]:
    """同一份年报被重新挂出来时（标题、披露时间、大小都相同，只是公告编号不同）只取一次。

    留编号最小的那条，别的编号记在它的 `relisted_as` 里。大小不明的不合并。
    """
    kept: dict[tuple, dict] = {}
    found: list[dict] = []
    for report in sorted(reports, key=lambda r: int(r["announcement_id"])):
        size = report.pop("listed_size_kb")
        key = (report["title"], report["announcement_time_ms"], size)
        first = kept.get(key) if size is not None else None
        if first is None:
            kept[key] = report
            found.append(report)
        else:
            first.setdefault("relisted_as", []).append(report["announcement_id"])
    return found
