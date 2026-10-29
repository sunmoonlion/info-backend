"""需求状态推送的契约 `knowfeed-supply-status/v1`（SDD 0045 第 2.5 节「需求状态只由 knowfeed
通知」、第七节块 4）。

knowfeed 是提供方：需求项的状态每变一次，经发件箱给提这张单的应用（knowbench、knowlab）
发一条；应用据此恢复任务或报失败，也照旧可以按单号随时查。一条通知带这张单当时的汇总
状态与每一项，应用以单号查到的为准：通知可能重发、可能晚到，只往前走的状态让应用
丢掉比手里旧的那条就行（按 `items[].updated_at`）。

接收端（应用那头）现在还没做，由 Opus 排：入口统一为 `RECEIVER_PATH`，knowfeed 以服务
身份调，关系 `supply-status`，scope `<应用>:supply-status`。

模型是唯一真源，父仓 `contracts/supply-status/v1/` 下的 schema 由它生成：
在后端目录下 `python -m app.domain.supply.schema`。
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CONTRACT = "knowfeed-supply-status/v1"
RECEIVER_PATH = "/api/internal/v1/knowfeed/demand-status"
RELATION = "supply-status"

ItemStatusV1 = Literal[
    "accepted",
    "fetching",
    "ready",
    "partial",
    "failed",
    "unsatisfiable",
    "cancelled",
    "timeout",
]


class DemandStatusItemV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    item_id: str = Field(min_length=1, max_length=64)
    confirmation_item: str = Field(
        min_length=1, max_length=16, description="确认里的条目编号（c1、c2……）"
    )
    status: ItemStatusV1
    reason: str | None = Field(
        default=None,
        max_length=200,
        description="结束的原因编码，如 no_channel、channel_has_none、readings_incomplete",
    )
    updated_at: datetime


class DemandStatusEventV1(BaseModel):
    """一张需求单的状态变了。"""

    model_config = ConfigDict(extra="forbid")

    contract: Literal["knowfeed-supply-status/v1"] = CONTRACT
    event_id: str = Field(min_length=1, max_length=64, description="每条通知不同")
    demand_id: str = Field(min_length=1, max_length=64)
    app: Literal["knowbench", "knowlab"] = Field(description="提这张单的应用")
    owner: str = Field(min_length=1, max_length=255, description="归属用户")
    task_ref: str | None = Field(default=None, max_length=255)
    status: ItemStatusV1 = Field(description="需求单的汇总状态")
    items: list[DemandStatusItemV1] = Field(min_length=1, max_length=50)
    occurred_at: datetime
