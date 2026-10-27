"""受理需求与切合（SDD 0045 第 2.4、第四节）：这一版只做精确描述。

先核什么（顺序照 0045 第 2.4 节）：
1. 同一幂等钥匙、内容相同的回原结果；同一钥匙内容不同的拒绝；
2. 格式；
3. 凭证：确认编号真、7 天内、是这个用户的，条目对得上（问 knowbase）；
4. 配额（每人每月新取份数等）随块 4 补齐，这一版不扣。

受理后正式切合：按来源目录里覆盖这条描述、审过、在用的渠道先后排，取第一个；建（或挂上
已有的）取数作业，同一渠道同一描述跨用户只取一次。没有渠道的转「无法满足」。
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from app.application.ports.supply import Knowbase, SupplyRepository
from app.domain.supply.models import (
    Demand,
    DemandItem,
    DemandItemIn,
    Job,
    SupplyError,
    check_demand,
)


class DemandService:
    def __init__(
        self,
        repo: SupplyRepository,
        knowbase: Knowbase,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        new_id: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self.repo = repo
        self.knowbase = knowbase
        self.now = clock
        self.new_id = new_id

    async def submit(
        self,
        *,
        app: str,
        owner: str,
        proposer: str,
        chain: tuple[str, ...],
        task_ref: str | None,
        confirmation_id: str,
        idempotency_key: str,
        items: list[DemandItemIn],
    ) -> tuple[Demand, list[DemandItem], bool]:
        """受理一张需求单；返回（需求单，需求项，是不是重放）。"""
        check_demand(owner, proposer, idempotency_key, items)
        digest = _digest(owner, proposer, chain, task_ref, confirmation_id, items)
        prior = await self.repo.find_demand(app, idempotency_key)
        if prior is not None:
            if prior.digest != digest:
                raise SupplyError(
                    409,
                    "idempotency_key_reused",
                    "this key was used for another demand",
                )
            return prior, await self.repo.items_of(prior.id), True
        confirmation = await self.knowbase.check_confirmation(confirmation_id, owner)
        confirmed = {str(i.get("item_id")): i for i in confirmation.get("items") or []}
        for item in items:
            given = confirmed.get(item.confirmation_item)
            if given is None or given.get("key") != item.key.as_dict():
                raise SupplyError(
                    403, "outside_confirmation", "an item is not in the confirmation"
                )
        now = self.now()
        demand = Demand(
            id=self.new_id(),
            owner=owner,
            proposer=proposer,
            chain=chain,
            app=app,
            task_ref=task_ref,
            confirmation_id=confirmation_id,
            idempotency_key=idempotency_key,
            digest=digest,
            created_at=now,
        )
        channels = await self.repo.channels()
        made: list[DemandItem] = []
        jobs: dict[str, Job] = {}
        links: dict[str, str] = {}
        for given in items:
            item_id = self.new_id()
            channel = next((c for c in channels if c.covers(given.key)), None)
            if channel is None:
                made.append(
                    DemandItem(
                        id=item_id,
                        demand_id=demand.id,
                        key=given.key,
                        readings=given.readings,
                        confirmation_item=given.confirmation_item,
                        status="unsatisfiable",
                        created_at=now,
                        updated_at=now,
                        reason="no channel can supply this yet",
                    )
                )
                continue
            dedup = f"fetch:{channel.id}:{given.key.text}"
            jobs.setdefault(
                dedup,
                Job(
                    id=self.new_id(),
                    kind="fetch",
                    channel_id=channel.id,
                    dedup_key=dedup,
                    payload={"key": given.key.as_dict()},
                    status="queued",
                    attempts=0,
                    created_at=now,
                    tier=1 if task_ref else 2,
                ),
            )
            links[item_id] = dedup
            made.append(
                DemandItem(
                    id=item_id,
                    demand_id=demand.id,
                    key=given.key,
                    readings=given.readings,
                    confirmation_item=given.confirmation_item,
                    status="fetching",
                    created_at=now,
                    updated_at=now,
                    channel_id=channel.id,
                )
            )
        await self.repo.submit(demand, made, list(jobs.values()), links)
        return demand, await self.repo.items_of(demand.id), False

    async def get(self, app: str, demand_id: str) -> tuple[Demand, list[DemandItem]]:
        demand = await self.repo.get_demand(demand_id)
        if demand is None or demand.app != app:
            raise SupplyError(404, "demand_not_found", "no such demand")
        return demand, await self.repo.items_of(demand.id)

    async def cancel(self, app: str, demand_id: str) -> int:
        """取消只摘自己这张单的需求项；共享的作业照跑，别人还在等。"""
        demand, _ = await self.get(app, demand_id)
        return await self.repo.cancel_items(demand.id, self.now())


def _digest(
    owner: str,
    proposer: str,
    chain: tuple[str, ...],
    task_ref: str | None,
    confirmation_id: str,
    items: list[DemandItemIn],
) -> str:
    canonical = json.dumps(
        {
            "owner": owner,
            "proposer": proposer,
            "chain": list(chain),
            "task_ref": task_ref,
            "confirmation_id": confirmation_id,
            "items": [
                {
                    "key": i.key.as_dict(),
                    "readings": sorted(i.readings),
                    "confirmation_item": i.confirmation_item,
                }
                for i in items
            ],
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
