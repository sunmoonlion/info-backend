"""巨潮资讯网（法定信息披露）作为目录型渠道的插件（SDD 0045 第 3.2 节、0049 第四节）。

插件只管映射与解析：描述 → 巨潮的公告分类与查询参数，公告列表 → 条目，条目 → 原件。
取数走框架给的 `Http`（只连公网、限大小与重定向），插件不自带网络客户端。

- 定期报告：一条描述对一份报告（那一年那一期），按标题认全文、摘要、英文版。
- 其余（临时公告、招股书、上市公告书、投资者关系活动记录、监管函件）：一条描述是一个
  披露时间窗，逐个查相关的巨潮分类，窗里的每条公告是一份资料；按巨潮分类给缺省事项，
  再按标题认细类、补事项（0049 第四节）。认不出细类的归临时公告，认不出事项的就空着。

接口地址与参数参考了 akshare 1.18.97（MIT License）的 stock_zh_a_disclosure_report_cninfo，
标题的过滤规矩沿用旧证券专线的采集器（2026-10-28 已删）在真实年报上试出来的。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone
from typing import Protocol

from app.application.supply.directory import SecurityEntry
from app.domain.securities.models import InvalidSecurityCode, SecurityCode
from app.domain.supply.models import DescriptionKey, Entry, SupplyError
from app.domain.supply.vocabulary import is_periodic, types_of

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
# 一条披露时间窗最多交这么多份（knowbase 一条运行授权的上限）；不限期间的按最近一年
MAX_WINDOW_ENTRIES = 200
DEFAULT_WINDOW = timedelta(days=365)

# 定期报告：细类 → 巨潮的公告分类、标题
_PERIODIC: dict[str, tuple[str, re.Pattern[str]]] = {
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
# 其余公告：巨潮分类 → 缺省事项（0049 第四节）
_CATEGORY_TOPIC: dict[str, str] = {
    "category_yjygjxz_szsh": "results",
    "category_qyfpxzcs_szsh": "dividend",
    "category_dshgg_szsh": "meetings",
    "category_jshgg_szsh": "meetings",
    "category_gddh_szsh": "meetings",
    "category_rcjy_szsh": "operations",
    "category_gszl_szsh": "governance",
    "category_zj_szsh": "intermediary",
    "category_sf_szsh": "ipo",
    "category_zf_szsh": "refinancing",
    "category_pg_szsh": "refinancing",
    "category_gszq_szsh": "refinancing",
    "category_kzzq_szsh": "refinancing",
    "category_qtrz_szsh": "refinancing",
    "category_gqjl_szsh": "equity",
    "category_jj_szsh": "equity",
    "category_gqbd_szsh": "equity",
    "category_bcgz_szsh": "trading",
    "category_cqdq_szsh": "trading",
    "category_fxts_szsh": "trading",
    "category_tbclts_szsh": "trading",
    "category_tszlq_szsh": "trading",
}
# 标题里的强信号（不论巨潮分类）：补事项
_TITLE_TOPICS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"重大资产重组|吸收合并|破产重整|重整计划|要约收购"), "restructuring"),
    (
        re.compile(r"问询函|关注函|监管工作函|警示函|监管措施|纪律处分|通报批评"),
        "supervision",
    ),
    (re.compile(r"业绩说明会"), "results"),
)
# 没有对应巨潮分类的事项，按标题关键词检索
_SEARCH_WORDS: dict[str, tuple[str, ...]] = {
    "restructuring": ("重大资产重组", "吸收合并", "破产重整", "要约收购"),
    "supervision": ("问询函", "关注函", "监管工作函", "警示函", "纪律处分"),
}
# 交易所、监管机构发给公司的原函（公司的回复与「收到……的公告」是临时公告）
_LETTER = re.compile(r"^关于对.{0,80}的(?:问询函|关注函|监管工作函|警示函)$")
# 能按描述采的非定期细类
_WINDOW_TYPES = frozenset(
    {
        "interim_announcement",
        "prospectus",
        "listing_announcement",
        "ir_activity",
        "regulatory_letter",
    }
)
# 已取消的不要；H 股的报告也挂在 A 股的分类下面，那不是 A 股的
_DROPPED = ("取消", "已取消", "H股", "H 股")
_REVISED = ("修订", "更正", "更新")


class Http(Protocol):
    async def post_form(self, url: str, form: tuple[tuple[str, str], ...]) -> bytes: ...

    async def get(self, url: str) -> bytes: ...

    async def download(
        self, url: str, *, max_bytes: int, timeout_seconds: float
    ) -> tuple[bytes, str]:
        """取一个文件：（内容，内容类型）。"""
        ...


def edition_of(title: str) -> str:
    """版本形态照标题认：摘要、英文版，其余全文。"""
    if "摘要" in title:
        return "summary"
    if "英文" in title or "english" in title.lower():
        return "english"
    return "full"


def type_of(title: str) -> str:
    """非定期公告的细类（0049 第四节）；认不出的是临时公告。"""
    if re.search(r"招股说明书|招股意向书", title):
        return "prospectus"
    if "上市公告书" in title:
        return "listing_announcement"
    if "投资者关系活动记录" in title:
        return "ir_activity"
    if _LETTER.search(title):
        return "regulatory_letter"
    return "interim_announcement"


def topics_of(title: str, category: str | None) -> tuple[str, ...]:
    """事项：巨潮分类给的缺省，加标题里的强信号；可多个，认不出就空。"""
    found: list[str] = []
    if category in _CATEGORY_TOPIC:
        found.append(_CATEGORY_TOPIC[category])
    for pattern, topic in _TITLE_TOPICS:
        if pattern.search(title) and topic not in found:
            found.append(topic)
    return tuple(found)


# 证券代码 → 巨潮的机构编号：整份证券列表一天取一次，同一进程里各作业共用（插件按渠道、
# 按作业建，不在实例上缓存，免得每个作业多取一次列表、多占一次限速）
ORG_IDS_TTL = timedelta(days=1)
_ORG_IDS: dict[str, object] = {"at": None, "ids": {}}


@dataclass
class CninfoCatalog:
    http: Http
    _org_ids: dict[str, str] | None = None

    async def list(self, key: DescriptionKey) -> list[Entry]:
        """这一条描述在巨潮能列出的条目（新的在前）；没有就是空。"""
        if is_periodic(key.doc_type):
            return await self._periodic(key)
        wanted = set(types_of(key.doc_type)) & _WINDOW_TYPES
        if not wanted:
            return []
        return await self._window(key, wanted)

    async def fetch(self, entry: Entry) -> tuple[bytes, str]:
        content, media_type = await self.http.download(
            entry.url,
            max_bytes=MAX_REPORT_BYTES,
            timeout_seconds=REPORT_TIMEOUT_SECONDS,
        )
        if not content.startswith(b"%PDF-"):
            raise SupplyError(502, "not_a_pdf", "the channel did not return a PDF")
        return content, "application/pdf"

    # —— 定期报告：一条对一份 ——

    async def _periodic(self, key: DescriptionKey) -> list[Entry]:
        if key.period_end is None:
            return []
        code = key.subject.split(".")[0]
        org_id = await self._org_id(code)
        category, title_re = _PERIODIC[key.doc_type]
        year = key.period_end.year
        # 报告期结束后才会披露；更正版可能晚一年多
        rows = await self._query(
            code, org_id, category, key.period_end, date(year + 2, 6, 30)
        )
        entries = [
            e
            for e in (self._report(key, r, title_re, year) for r, _ in rows)
            if e is not None
        ]
        return sorted(entries, key=lambda e: e.disclosed_on or date.min, reverse=True)

    def _report(
        self, key: DescriptionKey, row: dict, title_re: re.Pattern[str], year: int
    ) -> Entry | None:
        title, adjunct, announcement_id, disclosed = _row(row)
        if title is None or any(word in title for word in _DROPPED):
            return None
        matched = title_re.search(title)
        if matched is None or int(matched.group(1)) != year:
            return None
        edition = edition_of(title)
        if edition != key.edition:
            return None
        code = key.subject.split(".")[0]
        # 同一份报告的更正版是同一份资料的新版本：编号按报告，不按公告
        doc_id = f"{code}:{key.doc_type}:{year}"
        if edition != "full":
            doc_id += f":{edition}"
        return Entry(
            channel_id=CHANNEL_ID,
            channel_doc_id=doc_id,
            title=title,
            url=f"{_STATIC}/{adjunct}",
            disclosed_on=disclosed,
            revised=any(word in title for word in _REVISED),
            announcement_id=announcement_id,
            doc_type=key.doc_type,
            edition=edition,
        )

    # —— 其余：一条是一个披露时间窗 ——

    async def _window(self, key: DescriptionKey, wanted: set[str]) -> list[Entry]:
        code = key.subject.split(".")[0]
        org_id = await self._org_id(code)
        end = key.period_end or datetime.now(_SHANGHAI).date()
        start = key.period_start or end - DEFAULT_WINDOW
        found: dict[str, Entry] = {}
        for category, words in self._passes(key, wanted):
            for searchkey in words or ("",):
                for row, _ in await self._query(
                    code, org_id, category, start, end, searchkey=searchkey
                ):
                    entry = self._announcement(code, row, category or None)
                    if entry is None or entry.doc_type not in wanted:
                        continue
                    if entry.edition != key.edition:
                        continue
                    prior = found.get(entry.channel_doc_id)
                    if prior is not None:
                        merged = prior.topics + tuple(
                            t for t in entry.topics if t not in prior.topics
                        )
                        entry = Entry(**{**prior.__dict__, "topics": merged})
                    found[entry.channel_doc_id] = entry
        entries = [
            e
            for e in found.values()
            if not key.topics or set(e.topics) & set(key.topics)
        ]
        entries.sort(key=lambda e: e.disclosed_on or date.min, reverse=True)
        return entries[:MAX_WINDOW_ENTRIES]

    @staticmethod
    def _passes(
        key: DescriptionKey, wanted: set[str]
    ) -> list[tuple[str, tuple[str, ...]]]:
        """查哪几个巨潮分类（每个可带标题关键词）：只要招股书、上市公告书的查首发，
        只要投资者关系活动记录的查公司治理；要了事项的查对应分类，没有对应分类的事项
        按关键词检索；其余逐个查全部分类。"""
        if wanted <= {"prospectus", "listing_announcement"}:
            return [("category_sf_szsh", ())]
        if wanted == {"ir_activity"}:
            return [("category_gszl_szsh", ())]
        if wanted == {"regulatory_letter"}:
            return [("", _SEARCH_WORDS["supervision"])]
        if key.topics:
            passes: list[tuple[str, tuple[str, ...]]] = [
                (c, ()) for c, t in _CATEGORY_TOPIC.items() if t in key.topics
            ]
            for topic in key.topics:
                if topic in _SEARCH_WORDS:
                    passes.append(("", _SEARCH_WORDS[topic]))
            return passes
        return [(c, ()) for c in _CATEGORY_TOPIC] + [
            ("", _SEARCH_WORDS["restructuring"] + _SEARCH_WORDS["supervision"])
        ]

    def _announcement(self, code: str, row: dict, category: str | None) -> Entry | None:
        title, adjunct, announcement_id, disclosed = _row(row)
        if title is None or any(word in title for word in _DROPPED):
            return None
        return Entry(
            channel_id=CHANNEL_ID,
            # 每条公告是一份资料（更正公告另是一条）
            channel_doc_id=f"{code}:announcement:{announcement_id}",
            title=title,
            url=f"{_STATIC}/{adjunct}",
            disclosed_on=disclosed,
            revised=any(word in title for word in _REVISED),
            announcement_id=announcement_id,
            doc_type=type_of(title),
            edition=edition_of(title),
            topics=topics_of(title, category),
        )

    # —— 查询 ——

    async def _query(
        self,
        code: str,
        org_id: str,
        category: str,
        start: date,
        end: date,
        *,
        searchkey: str = "",
    ) -> list[tuple[dict, str]]:
        rows: list[tuple[dict, str]] = []
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
                        ("searchkey", searchkey),
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
            rows.extend((r, category) for r in found if isinstance(r, dict))
            total = payload.get("totalAnnouncement")
            if not isinstance(total, int) or page * _PAGE_SIZE >= total or not found:
                break
        return rows

    async def _org_id(self, code: str) -> str:
        if self._org_ids is None:
            fetched_at = _ORG_IDS["at"]
            now = datetime.now(UTC)
            if isinstance(fetched_at, datetime) and now - fetched_at < ORG_IDS_TTL:
                self._org_ids = dict(_ORG_IDS["ids"])  # type: ignore[call-overload]
            else:
                stocks = await self._stock_list()
                self._org_ids = {
                    str(s.get("code")): str(s.get("orgId"))
                    for s in stocks
                    if _ORG_ID.fullmatch(str(s.get("orgId") or ""))
                }
                _ORG_IDS.update(at=now, ids=dict(self._org_ids))
        if code not in self._org_ids:
            raise SupplyError(404, "security_not_found_at_source", "not on cninfo")
        return self._org_ids[code]

    async def securities(self) -> list[SecurityEntry]:
        """证券名录（查主体用）：沪深京 A 股的代码、简称、拼音首字母。"""
        out: list[SecurityEntry] = []
        for s in await self._stock_list():
            name = str(s.get("zwjc") or "").strip()
            if s.get("category") != "A股" or not name or len(name) > 64:
                continue
            try:
                code = SecurityCode(str(s.get("code") or ""))
            except InvalidSecurityCode:
                continue
            pinyin = str(s.get("pinyin") or "").strip().lower()
            out.append(SecurityEntry(f"{code.code}.{code.market}", name, pinyin[:32]))
        return out

    async def _stock_list(self) -> list[dict]:
        payload = _json(
            await self.http.get(f"{_BASE}/new/data/szse_stock.json"), "stock list"
        )
        stocks = payload.get("stockList") if isinstance(payload, dict) else None
        if not isinstance(stocks, list):
            raise SupplyError(502, "unexpected_response", "stock list")
        return [s for s in stocks if isinstance(s, dict)]


def _row(row: dict) -> tuple[str | None, str, str, date | None]:
    """（标题，附件路径，公告编号，披露日）；不合格的标题回 None。"""
    title = re.sub(r"</?em>", "", str(row.get("announcementTitle") or ""))
    adjunct = str(row.get("adjunctUrl") or "")
    announcement_id = str(row.get("announcementId") or "")
    time_ms = row.get("announcementTime")
    if (
        not title
        or not _ADJUNCT.fullmatch(adjunct)
        or not announcement_id.isdecimal()
        or not isinstance(time_ms, int)
    ):
        return None, adjunct, announcement_id, None
    disclosed = (
        datetime.fromtimestamp(time_ms / 1000, tz=UTC).astimezone(_SHANGHAI).date()
    )
    return title, adjunct, announcement_id, disclosed


def _json(content: bytes, what: str):
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise SupplyError(502, "unexpected_response", what) from None
