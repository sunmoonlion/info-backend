"""采集申请的接口形状（0008-info-intake）。用户面与管理面分开：用户面不露别人是谁、谁批的。"""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------- 入参
class SubmitRequest(Strict):
    security_code: str = Field(min_length=1, max_length=16)
    reason: str | None = Field(default=None, max_length=2000)
    # 从别的应用带过来的；不可信，服务端逐个校验，不合规则的当作没带
    source: str | None = Field(default=None, max_length=64, alias="from")
    ref: str | None = Field(default=None, max_length=256)

    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class ApproveRequest(Strict):
    add_to_watchlist: bool = False


class RejectRequest(Strict):
    note: str = Field(max_length=2000)


class WatchRequest(Strict):
    security_code: str = Field(min_length=1, max_length=16)
    note: str | None = Field(default=None, max_length=2000)


class UnwatchRequest(Strict):
    note: str | None = Field(default=None, max_length=2000)


# ---------------------------------------------------------------- 出参
class DatasetRead(BaseModel):
    dataset_id: str
    data_version: str
    start_date: str
    end_date: str
    # 去 knowledge 看这个数据集的地址，语言段留给页面填；没配就是空
    catalog_url_template: str | None = None


class OriginRead(BaseModel):
    app: str
    ref: str | None = None
    # 回到原处的地址。只从 info 自己的配置里来，从不从链接里来；没配就是空
    return_url: str | None = None


class MineRead(BaseModel):
    reason: str | None
    origin: OriginRead | None
    requested_at: datetime
    withdrawn: bool


class SecurityRequestRead(BaseModel):
    """给申请人看的。"""

    id: uuid.UUID
    security_code: str
    kind: str
    progress: str
    created_at: datetime
    closed_at: datetime | None
    requesters: int  # 还有几个人也要（含自己）
    mine: MineRead | None
    can_withdraw: bool
    rejection_note: str | None  # 被拒绝时管理员写的原因
    dataset: DatasetRead | None


class AvailabilityRead(BaseModel):
    security_code: str
    market: str
    in_watchlist: bool
    dataset: DatasetRead | None
    open_request: SecurityRequestRead | None
    mine: bool
    my_open_count: int
    max_open: int
    needs_approval: bool  # 现在提交要不要等批准


class RequesterAdminRead(BaseModel):
    actor_id: uuid.UUID
    reason: str | None
    origin: OriginRead | None
    requested_at: datetime
    withdrawn_at: datetime | None


class SecurityRequestAdminRead(BaseModel):
    """给管理员看的。"""

    id: uuid.UUID
    security_code: str
    kind: str
    status: str
    progress: str
    open: bool
    created_at: datetime
    closed_at: datetime | None
    decided_by: str | None
    decided_at: datetime | None
    decision_note: str | None
    ingestion_id: uuid.UUID | None
    in_watchlist: bool
    requesters: list[RequesterAdminRead]
    dataset: DatasetRead | None


class WatchlistEntryRead(BaseModel):
    security_code: str
    added_at: datetime
    added_by: str
    note: str | None
    removed_at: datetime | None
    removed_by: str | None
    removal_note: str | None
