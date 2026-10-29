"""需求状态推送（SDD 0045 第 2.5 节「需求状态只由 knowfeed 通知」，第七节块 4）。

需求项的状态每变一次，记录那头在同一个事务里经发件箱排一条通知（契约
`knowfeed-supply-status/v1`，`domain/supply/status_event.py`）；worker 取出来交给提这张单的
应用。发不出去（对方暂时不可用）就抛错，由可靠投递按退避重试。

接收端还没做（knowbench、knowlab 现在按单号查）：哪个应用的这条关系没配，就记一条日志
跳过，不重试、不堆积；配上以后，从那时起的变化照发。应用以按单号查到的为准。
"""

from __future__ import annotations

import logging

from app.application.ports.supply import StatusSender
from app.domain.supply.status_event import DemandStatusEventV1

log = logging.getLogger(__name__)


class StatusNotifier:
    def __init__(self, sender: StatusSender) -> None:
        self.sender = sender

    async def deliver(self, payload: dict[str, object]) -> str:
        """回 sent 或 skipped（那个应用的接收关系没配）。"""
        event = DemandStatusEventV1.model_validate(payload)
        if not self.sender.configured(event.app):
            log.info(
                "supply_status_not_sent app=%s demand=%s reason=relation_unconfigured",
                event.app,
                event.demand_id,
            )
            return "skipped"
        await self.sender.send(event.app, event.model_dump(mode="json"))
        return "sent"
