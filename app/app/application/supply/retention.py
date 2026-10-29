"""原件留存到期（SDD 0045 第 3.4 节，第七节块 7）：原件交 knowbase 拿到回执后再留 30 天，
然后删对象存储里的内容，只留出处与摘要（`supply_original` 那一行留着，记删的时间）。

由 beat 每小时触发一次（`app.tasks.supply_upkeep.purge_originals`），一次删一批。删不掉的
（对象存储暂时不可用）留到下一轮；对象早就不在了的照删掉记。真源在 knowbase，以后要
重新加工从 knowbase 取原件。注销、依法删除个人信息时不等 30 天就要真删（0043 第 2.8 节），
那随注销流程做，不在这里。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import UTC, datetime

from app.application.ports.supply import ObjectStore, RetentionRepository

log = logging.getLogger(__name__)
BATCH = 200


class OriginalRetention:
    def __init__(
        self,
        repo: RetentionRepository,
        objects: ObjectStore,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.repo = repo
        self.objects = objects
        self.now = clock

    async def purge_due(self, limit: int = BATCH) -> int:
        """删到期原件的内容；回删了几份。"""
        purged = 0
        for original_id, object_key in await self.repo.due_originals(self.now(), limit):
            try:
                await self.objects.delete(object_key)
            except Exception as exc:  # 下一轮再删；不让一份卡住整批
                log.warning(
                    "supply_original_purge_failed original=%s error=%s",
                    original_id,
                    type(exc).__name__,
                )
                continue
            if await self.repo.mark_purged(original_id, self.now()):
                purged += 1
        return purged
