"""本人在 knowfeed web端亲手提需求、看「我的需求」、取消、再试一次（SDD 0045 第 2.3–2.5 节，
第七节块 10；发版前补齐 F1）。

- 亲手提：本人提交即确认。knowfeed 凭本人令牌请 knowbase 记确认（0045 第 2.4 节「本人亲手
  提：本人令牌，knowfeed 凭它请 knowbase 记确认」），拿到确认编号与入口分支：本人已经齐了的
  （`ready`）不进 knowfeed，其余照应用代提的同一套受理（核确认、配额、切合）。
- 「最晚什么时候要」亲手提的必填（0045 第 2.2 节）；过了还没办完的转「超时」。
- 同一幂等钥匙重放回原来那张（本人点了两次「提交」），不再记第二次确认。
- 「我的需求」列本人名下的全部需求单，不论是自己提的还是应用、专家、订阅代提的；本人都能
  取消（0045 第 2.5 节「用户或应用取消」）。
- 「再试一次」：失败、超时、无法满足的项，本人再确认一次，生成挂在原项下的新项（新的
  幂等钥匙）。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime

from app.application.ports.supply import KnowbaseUser, SupplyRepository
from app.application.supply.demands import DemandService
from app.domain.supply.models import (
    Demand,
    DemandItem,
    DemandItemIn,
    SupplyError,
    refused,
)

APP = "knowfeed"
RETRYABLE = frozenset({"failed", "timeout", "unsatisfiable"})
MAX_LISTED = 100
# 确认条目只写一种读法（knowbase `ConfirmItemIn.reading`）：要了几种的写最费事的那一种
_READING_ORDER = ("table", "pages", "text", "original")


def confirm_item(item: DemandItemIn) -> dict[str, object]:
    """需求项写成 knowbase 确认的一条。"""
    if item.key is not None:
        reading = next(r for r in _READING_ORDER if r in item.readings)
        return {"key": item.key.as_dict(), "reading": reading}
    # 给网址的确认连范围一起记：连不连它直接链到的文件（二审第 10 条）
    out: dict[str, object] = {"url": item.url, "follow_links": item.follow_links}
    title = item.prefill.get("title")
    if isinstance(title, str) and title:
        out["title"] = title[:200]
    return out


class MyDemands:
    def __init__(
        self,
        demands: DemandService,
        repo: SupplyRepository,
        user: KnowbaseUser | None,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self.demands = demands
        self.repo = repo
        self.user = user
        self.now = clock

    async def submit(
        self,
        token: str,
        owner: str,
        *,
        kind: str,
        items: list[DemandItemIn],
        deadline: datetime | None,
        note: str | None,
        idempotency_key: str,
        conditions: dict[str, object] | None = None,
        retry_of: dict[int, str] | None = None,
    ) -> tuple[Demand | None, list[DemandItem], bool, list[str]]:
        """回（需求单，需求项，是不是重放，本人已有、不必取的确认条目）。全都已有时没有
        需求单。`retry_of`：第几项 → 挂在哪一项下。

        同一（用户，幂等钥匙）先占位再去 knowbase 记确认（二审第 12 条）：并发的第二次
        不再记一份孤儿确认；内容不同的回 409；全都已有的也落一条记录，重放照样不再记确认。
        摘要按请求内容算，不含确认编号。"""
        if kind not in ("description", "url"):
            raise refused("kind_invalid", "kind is description or url")
        if not items:
            raise refused("items_invalid", "give at least one item")
        if any((i.key is None) != (kind == "url") for i in items):
            raise refused("item_invalid", "all items are of the chosen kind")
        if kind == "url" and not all(i.declared_public for i in items):
            # 用户声明「公开页面、不用登录」才收（0045 第 2.2 节），照记进需求项
            raise refused("public_declaration_required", "declare the pages public")
        if deadline is None:
            raise refused("deadline_required", "say when you need it by")
        digest = _digest(kind, items, deadline, note, conditions, retry_of)
        prior = await self.repo.claim_submission(
            owner, idempotency_key, digest, self.now()
        )
        resumed_confirmation: dict[str, object] | None = None
        if prior is not None:
            if prior["digest"] != digest:
                raise SupplyError(
                    409,
                    "idempotency_key_reused",
                    "this key was used for another request",
                )
            resumed = prior.get("resumed")
            if isinstance(resumed, dict) and isinstance(resumed.get("confirmed"), dict):
                resumed_confirmation = dict(resumed["confirmed"])
            else:
                return await self._replay(prior, digest)
        confirmed_holder: dict[str, object] = {}
        if resumed_confirmation is not None:
            confirmed_holder["confirmed"] = resumed_confirmation
        try:
            if deadline <= self.now():
                raise refused("deadline_invalid", "the deadline is in the past")
            made = await self._submit(
                token,
                owner,
                kind=kind,
                items=items,
                deadline=deadline,
                note=note,
                idempotency_key=idempotency_key,
                conditions=conditions,
                retry_of=retry_of,
                confirmed_holder=confirmed_holder,
            )
        except Exception:
            if "confirmed" not in confirmed_holder:
                await self.repo.drop_submission(owner, idempotency_key)
            raise
        demand, _, _, ready = made
        await self.repo.finish_submission(
            owner, idempotency_key, demand.id if demand else None, {"ready": ready}
        )
        return made

    async def _replay(
        self, prior: dict[str, object], digest: str
    ) -> tuple[Demand | None, list[DemandItem], bool, list[str]]:
        if prior["digest"] != digest:
            raise SupplyError(
                409, "idempotency_key_reused", "this key was used for another request"
            )
        result = prior.get("result")
        if not isinstance(result, dict) or "ready" not in result:
            raise SupplyError(
                409, "submission_in_progress", "the same request is being submitted"
            )
        ready = [str(r) for r in result.get("ready") or []]
        demand_id = prior.get("demand_id")
        if not demand_id:
            return None, [], True, ready
        demand = await self.repo.get_demand(str(demand_id))
        assert demand is not None
        return demand, await self.repo.items_of(demand.id), True, ready

    async def _submit(
        self,
        token: str,
        owner: str,
        *,
        kind: str,
        items: list[DemandItemIn],
        deadline: datetime,
        note: str | None,
        idempotency_key: str,
        conditions: dict[str, object] | None,
        retry_of: dict[int, str] | None,
        confirmed_holder: dict[str, object] | None = None,
    ) -> tuple[Demand | None, list[DemandItem], bool, list[str]]:
        existing = (confirmed_holder or {}).get("confirmed")
        if isinstance(existing, dict):
            confirmation = existing
        else:
            if self.user is None:
                raise SupplyError(
                    503, "knowbase_unconfigured", "knowbase is not set up"
                )
            confirmation = await self.user.confirm(
                token,
                kind=kind,
                items=[confirm_item(i) for i in items],
                supply={"conditions": conditions or {}},
            )
            if confirmed_holder is not None:
                confirmed_holder["confirmed"] = confirmation
            await self.repo.save_submission_progress(
                owner, idempotency_key, {"confirmed": confirmation}
            )
        ids = [str(i.get("item_id")) for i in confirmation.get("items") or []]
        if len(ids) != len(items):
            raise SupplyError(502, "unexpected_response", "knowbase confirmation")
        branches = {
            str(b.get("item_id")): str(b.get("branch"))
            for b in confirmation.get("branches") or []
        }
        ready = [cid for cid in ids if branches.get(cid) == "ready"]
        to_supply = [
            replace(item, confirmation_item=cid)
            for item, cid in zip(items, ids, strict=True)
            if branches.get(cid) != "ready"
        ]
        if not to_supply:
            return None, [], False, ready
        back = {
            ids[n]: item_id for n, item_id in (retry_of or {}).items() if n < len(ids)
        }
        demand, made, replayed = await self.demands.submit(
            app=APP,
            owner=owner,
            proposer="user",
            chain=(),
            task_ref=None,
            confirmation_id=str(confirmation["confirmation_id"]),
            idempotency_key=idempotency_key,
            items=to_supply,
            deadline=deadline,
            note=note,
            retry_of=back,
        )
        return demand, made, replayed, ready

    async def retry(
        self,
        token: str,
        owner: str,
        item_id: str,
        *,
        deadline: datetime | None,
        idempotency_key: str,
    ) -> tuple[Demand | None, list[DemandItem], bool, list[str]]:
        found = await self.repo.item(item_id)
        if found is None or found[1].owner != owner:
            raise SupplyError(404, "item_not_found", "no such item")
        item, _ = found
        if item.status not in RETRYABLE:
            raise SupplyError(
                409, "item_not_retryable", "only failed, timed out or unsatisfiable"
            )
        again = DemandItemIn(
            item.key,
            item.readings,
            "c1",
            url=item.url,
            follow_links=item.follow_links,
            prefill=dict(item.prefill),
            declared_public=item.declared_public or item.url is None,
        )
        return await self.submit(
            token,
            owner,
            kind="url" if item.key is None else "description",
            items=[again],
            deadline=deadline,
            note=None,
            idempotency_key=idempotency_key,
            retry_of={0: item.id},
        )

    async def list(self, owner: str) -> list[tuple[Demand, list[DemandItem]]]:
        return await self.repo.demands_of(owner, MAX_LISTED)


def _digest(
    kind: str,
    items: list[DemandItemIn],
    deadline: datetime,
    note: str | None,
    conditions: dict[str, object] | None,
    retry_of: dict[int, str] | None,
) -> str:
    """请求内容的摘要（不含确认编号）：同一钥匙内容一样的才算重放。"""
    body = {
        "kind": kind,
        "items": [
            {
                "key": i.key.as_dict() if i.key is not None else None,
                "url": i.url,
                "follow_links": i.follow_links,
                "prefill": i.prefill,
                "declared_public": i.declared_public,
                "readings": sorted(i.readings),
            }
            for i in items
        ],
        "deadline": deadline.isoformat(),
        "note": note,
        "conditions": conditions or {},
        "retry_of": {str(k): v for k, v in (retry_of or {}).items()},
    }
    text = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
