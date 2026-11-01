"""「看新的」（SDD 0045 第 3.2 节；第七节块 9）：订阅每一轮问渠道游标之后新出了什么。

- 目录型（巨潮这类）没有专门的「新的」接口，用「列」做：非定期的类型列游标那天（往前
  留三天重叠）到今天的披露时间窗；定期报告列最近结束的两个报告期。见过的按游标去掉。
  更正版是同一份资料的新版本：按「渠道内编号 + 这一次公告的编号」认是不是见过。
- 订阅源型、栏目页见 `web.py` 的 `WebSite.watch`。

订同一个对象（「主体|类型」、网址）的人共用一个游标（0045 第 3.3 节），各订阅各记进度
（`subscriptions.py`）。
"""

from __future__ import annotations

from datetime import date, timedelta

from app.application.ports.supply import Catalog
from app.domain.supply.models import DescriptionKey, Entry
from app.domain.supply.vocabulary import is_periodic

FIRST_LOOK = timedelta(days=30)  # 第一次看，往回看多久
OVERLAP = timedelta(days=3)  # 披露时间可能晚登记几天：每次往前重叠几天
SEEN_KEPT = 2000
# 定期报告的报告期末（月、日）
PERIOD_END: dict[str, tuple[int, int]] = {
    "annual_report": (12, 31),
    "semiannual_report": (6, 30),
    "q1_report": (3, 31),
    "q3_report": (9, 30),
}


def found_id(entry: Entry) -> str:
    """见没见过按这个认：更正版（另一次公告）算新的。"""
    if entry.announcement_id:
        return f"{entry.channel_doc_id}#{entry.announcement_id}"[:200]
    return entry.channel_doc_id[:200]


def recent_periods(doc_type: str, today: date, count: int = 2) -> list[date]:
    """最近结束的几个报告期末（新的在前）。"""
    month, day = PERIOD_END[doc_type]
    ends = [
        date(year, month, day)
        for year in range(today.year, today.year - 3, -1)
        if date(year, month, day) < today
    ]
    return ends[:count]


async def watch_catalog(
    catalog: Catalog, target: str, cursor: dict[str, object], today: date
) -> tuple[list[tuple[Entry, DescriptionKey]], dict[str, object]]:
    """目录型渠道的「看新的」：（新的条目连同列出它的那条描述，新游标）。`target` 是
    「主体|类型」。"""
    subject, doc_type = target.split("|", 1)
    if is_periodic(doc_type):
        keys = [
            DescriptionKey(subject, doc_type, date(end.year, 1, 1), end)
            for end in recent_periods(doc_type, today)
        ]
    else:
        since_text = cursor.get("since")
        since = (
            date.fromisoformat(str(since_text)) if since_text else today - FIRST_LOOK
        )
        keys = [DescriptionKey(subject, doc_type, since - OVERLAP, today)]
    listed: list[tuple[Entry, DescriptionKey]] = []
    for key in keys:
        listed += [(e, key) for e in await catalog.list(key)]
    seen_list = cursor.get("seen")
    seen = (
        {s for s in seen_list if isinstance(s, str)}
        if isinstance(seen_list, list)
        else set()
    )
    fresh = [(e, k) for e, k in listed if found_id(e) not in seen]
    ids = [found_id(e) for e, _ in fresh] + [
        s
        for s in (seen_list if isinstance(seen_list, list) else [])
        if isinstance(s, str)
    ]
    return fresh, {
        "seen": list(dict.fromkeys(ids))[:SEEN_KEPT],
        "since": today.isoformat(),
        "checked": True,
    }
