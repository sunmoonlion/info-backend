"""巨潮互动易（深交所上市公司的投资者问答）作为目录型渠道的插件（SDD 0045 第 3.2 节；
0049 第三节「互动问答 `ir_qa`：交易所互动平台，以后另接渠道」；发版前补齐 F9）。

插件只管映射与解析：描述 → 互动易的公司问答列表，问答 → 条目，条目 → 原件。取数走框架
给的 `Http`（只连公网、限大小与重定向、经限速），插件不自带网络客户端。

- 一条描述是一个披露时间窗（同其余非定期类型），窗里**已回答**的每条问答是一份资料，
  披露时间按回答时间（北京时间）。没回答的只是投资者的提问，不是公司说的，不要。
- 「取」是再问一次这条问答的详情，按固定的格式写成 Markdown（标题、提问与回答的时间、
  出处链接、问、答），一条问答一份；**不写提问者的账号**（是投资者个人的，与公司披露无关）。
  原件就是这份 Markdown：互动易给的是接口数据不是文件，这是我们照原样排出来的文本。
- 只有深市公司（渠道登记的 `config.markets` 是 `["SZ"]`）；沪市的投资者问答在上证 e 互动，
  另接渠道（见交回报告）。

接口照 akshare 1.18.97（MIT License）的 stock_irm_cninfo、stock_irm_ans_cninfo；2026-10-29
实测：公司问答列表只回最近的 20 条，起止日期参数不起作用，所以窗口早于这 20 条的，这条
渠道列不全（照实列出能看到的，交回报告里写明）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta, timezone

from app.application.supply.cninfo import Http
from app.domain.supply.models import DescriptionKey, Entry, SupplyError
from app.domain.supply.vocabulary import types_of

_BASE = "https://irm.cninfo.com.cn"
_SHANGHAI = timezone(timedelta(hours=8))
DEFAULT_WINDOW = timedelta(days=365)
PAGE_SIZE = 100
MAX_TEXT = 20_000  # 一条问答最多留这么多字（接口给的远小于这个）
DETAIL_MAX_BYTES = 2 * 1024 * 1024
DETAIL_TIMEOUT_SECONDS = 30.0


@dataclass
class CninfoIrmCatalog:
    http: Http
    channel_id: str = "cninfo-irm"

    async def list(self, key: DescriptionKey) -> list[Entry]:
        """窗里已回答的问答（新的在前）；这家不在互动易上、窗里没有，都是空。"""
        if "ir_qa" not in types_of(key.doc_type) or key.edition != "full":
            return []
        code = key.subject.split(".")[0]
        secid = await self._secid(code)
        if secid is None:
            return []
        payload = _json(
            await self.http.post_form(
                f"{_BASE}/newircs/company/question",
                (
                    ("stockcode", code),
                    ("orgId", secid),
                    ("pageSize", str(PAGE_SIZE)),
                    ("pageNum", "1"),
                    ("keyWord", ""),
                    ("startDay", ""),
                    ("endDay", ""),
                ),
            ),
            "question list",
        )
        rows = payload.get("rows") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise SupplyError(502, "unexpected_response", "question list")
        end = key.period_end or datetime.now(_SHANGHAI).date()
        start = key.period_start or end - DEFAULT_WINDOW
        entries = []
        for row in rows:
            entry = self._entry(code, row)
            if entry is None or entry.disclosed_on is None:
                continue
            if start <= entry.disclosed_on <= end:
                entries.append(entry)
        entries.sort(key=lambda e: e.disclosed_on or date.min, reverse=True)
        return entries

    async def fetch(self, entry: Entry) -> tuple[bytes, str]:
        """问一次详情，排成一份 Markdown。"""
        question_id = entry.announcement_id or ""
        # 取一份走 download：限速按它算日上限的一份
        content, _ = await self.http.download(
            f"{_BASE}/newircs/question/getQuestionDetail?questionId={question_id}",
            max_bytes=DETAIL_MAX_BYTES,
            timeout_seconds=DETAIL_TIMEOUT_SECONDS,
        )
        payload = _json(content, "question detail")
        data = payload.get("data") if isinstance(payload, dict) else None
        if (
            not isinstance(data, dict)
            or not str(data.get("replyContent") or "").strip()
        ):
            raise SupplyError(502, "unexpected_response", "question detail")
        return render(data, entry.url).encode("utf-8"), "text/markdown; charset=utf-8"

    async def _secid(self, code: str) -> str | None:
        payload = _json(
            await self.http.post_form(
                f"{_BASE}/newircs/index/queryKeyboardInfo", (("keyWord", code),)
            ),
            "company lookup",
        )
        found = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(found, list):
            raise SupplyError(502, "unexpected_response", "company lookup")
        for row in found:
            if isinstance(row, dict) and str(row.get("stockCode")) == code:
                secid = str(row.get("secid") or "")
                return secid if secid.isalnum() and len(secid) <= 32 else None
        return None

    def _entry(self, code: str, row: object) -> Entry | None:
        if not isinstance(row, dict):
            return None
        question_id = str(row.get("indexId") or "")
        answer = str(row.get("attachedContent") or "").strip()
        if not question_id.isdecimal() or not answer:
            return None
        answered = _day(row.get("updateDate")) or _day(row.get("pubDate"))
        name = str(row.get("companyShortName") or code)
        question = " ".join(str(row.get("mainContent") or "").split())
        return Entry(
            channel_id=self.channel_id,
            # 每条问答是一份资料
            channel_doc_id=f"{code}:irm:{question_id}",
            title=f"{name}互动易问答：{question[:60]}",
            url=f"{_BASE}/ircs/question/questionDetail?questionId={question_id}",
            disclosed_on=answered,
            announcement_id=question_id,
            doc_type="ir_qa",
        )


def render(data: dict, url: str) -> str:
    """一条问答排成 Markdown（固定格式，同一条问答每次排出来一字不差）。"""
    name = str(data.get("shortName") or "").strip()
    code = str(data.get("stockCode") or "").strip()
    question = str(data.get("questionContent") or "").strip()[:MAX_TEXT]
    answer = str(data.get("replyContent") or "").strip()[:MAX_TEXT]
    lines = [
        f"# {name}（{code}）互动易问答",
        "",
        f"- 提问时间：{_stamp(data.get('questionDate'))}",
        f"- 回答时间：{_stamp(data.get('replyDate'))}",
        f"- 出处：{url}",
        "",
        "## 问",
        "",
        question,
        "",
        f"## 答（{name}）",
        "",
        answer,
        "",
    ]
    return "\n".join(lines)


def _day(value: object) -> date | None:
    if not isinstance(value, int) or value <= 0:
        return None
    return datetime.fromtimestamp(value / 1000, tz=UTC).astimezone(_SHANGHAI).date()


def _stamp(value: object) -> str:
    if not isinstance(value, int) or value <= 0:
        return "未写明"
    moment = datetime.fromtimestamp(value / 1000, tz=UTC).astimezone(_SHANGHAI)
    return moment.strftime("%Y-%m-%d %H:%M")


def _json(content: bytes, what: str):
    try:
        return json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise SupplyError(502, "unexpected_response", what) from None
