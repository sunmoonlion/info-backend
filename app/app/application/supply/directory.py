"""证券名录（SDD 0043 第 3.1 节「查主体」，decisions ⑭）。

委托第 0 步，模型按名字查证券代码，免得自己编：knowbench 以服务身份来查，这里按
代码、简称、拼音找。名录取自巨潮的证券列表（沪深京 A 股），存在本库；查的时候名录
空着或超过一天没刷新，就排一次刷新（发件箱，worker 去取），这次先回库里现有的。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol

STALE_AFTER = timedelta(days=1)
MAX_RESULTS = 20
_QUERY = re.compile(r"^[\w一-鿿 .*-]{1,40}$")


@dataclass(frozen=True)
class SecurityEntry:
    subject: str  # 带市场后缀的代码，例如 600519.SH（与 knowbase 词表的主体格式一致）
    name: str  # 简称
    pinyin: str = ""


class DirectoryRepository(Protocol):
    async def search(self, query: str, limit: int) -> list[SecurityEntry]: ...

    async def refreshed_at(self) -> datetime | None: ...

    async def request_refresh(self, day: str) -> None: ...

    async def replace(self, entries: list[SecurityEntry], at: datetime) -> int: ...


class SecuritySource(Protocol):
    async def securities(self) -> list[SecurityEntry]: ...


class SecurityDirectory:
    def __init__(
        self,
        repo: DirectoryRepository,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repo = repo
        self.now = clock

    async def search(self, query: str, limit: int = MAX_RESULTS) -> list[SecurityEntry]:
        """按代码（六位或带后缀）、简称（含就算）、拼音首字母找；名录旧了顺手排刷新。"""
        text = query.strip()
        if not _QUERY.fullmatch(text):
            return []
        refreshed = await self.repo.refreshed_at()
        now = self.now()
        if refreshed is None or now - refreshed > STALE_AFTER:
            await self.repo.request_refresh(now.date().isoformat())
        return await self.repo.search(text, max(1, min(limit, MAX_RESULTS)))

    async def refresh(self, source: SecuritySource) -> int:
        """worker 里跑：取整份列表，整份换掉。取不到就不动库里的。"""
        entries = await source.securities()
        if not entries:
            return 0
        return await self.repo.replace(entries, self.now())
