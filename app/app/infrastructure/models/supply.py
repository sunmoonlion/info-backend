"""供给的表（SDD 0045 第五节）：渠道与审核、试采记录，需求单与需求项、作业与尝试、留 30 天
的原件。"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.models.base import Base


class SupplySecurity(Base):
    """证券名录（查主体用，SDD 0043 第 3.1 节）：巨潮的证券列表，整份换、一天至多一次。"""

    __tablename__ = "supply_security"

    subject: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    pinyin: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    refreshed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class SupplyChannel(Base):
    __tablename__ = "supply_channel"

    id: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    plugin: Mapped[str] = mapped_column(String(40), nullable=False)
    doc_types: Mapped[list] = mapped_column(JSONB, nullable=False)
    readings: Mapped[list] = mapped_column(JSONB, nullable=False)
    license: Mapped[str] = mapped_column(String(16), nullable=False)
    review: Mapped[str] = mapped_column(String(16), nullable=False)
    rate_per_minute: Mapped[int] = mapped_column(Integer, nullable=False)
    daily_cap: Mapped[int] = mapped_column(Integer, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0
    )
    # 0020 起（来源目录，0045 第 3.1 节）：网站、条款、规矩、状态原因、适配器配置
    domain: Mapped[str | None] = mapped_column(String(253))
    terms_url: Mapped[str | None] = mapped_column(Text)
    render_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    obey_robots: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    status_reason: Mapped[str | None] = mapped_column(String(200))
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # 0021 起：同时最多跑几个作业（登记的并发，0045 第 3.3 节）
    max_running: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # 0022 起：审核定的「区」：法定披露 / 为用户采集（0045 第 3.5 节）
    zone: Mapped[str] = mapped_column(String(16), nullable=False, default="collected")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "kind IN ('catalog', 'feed', 'web', 'search')",
            name="ck_supply_channel_kind",
        ),
        CheckConstraint(
            "review IN ('approved', 'pending', 'rejected')",
            name="ck_supply_channel_review",
        ),
        CheckConstraint(
            "status IN ('active', 'paused', 'disabled')",
            name="ck_supply_channel_status",
        ),
        CheckConstraint(
            "zone IN ('disclosure', 'collected')", name="ck_supply_channel_zone"
        ),
        Index(
            "uq_supply_channel_domain",
            "domain",
            unique=True,
            postgresql_where=text("domain IS NOT NULL"),
        ),
    )


class SupplyChannelEvent(Base):
    """审核记录与渠道上的每一次改动：谁、何时、做了什么、凭什么（0045 第 3.1 节）。"""

    __tablename__ = "supply_channel_event"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    channel_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("supply_channel.id"), nullable=False
    )
    action: Mapped[str] = mapped_column(String(24), nullable=False)
    actor: Mapped[str] = mapped_column(String(255), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))
    detail: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (Index("ix_supply_channel_event_channel", "channel_id", "at"),)


class SupplyChannelTrial(Base):
    """试采：放行之前对一条样例描述跑一次「列」和「取」（0045 第 3.1 节）。"""

    __tablename__ = "supply_channel_trial"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    channel_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("supply_channel.id"), nullable=False
    )
    key: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    listed: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    fetched: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(String(200))
    requested_by: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'ok', 'failed')",
            name="ck_supply_channel_trial_status",
        ),
        Index("ix_supply_channel_trial_channel", "channel_id", "created_at"),
    )


class SupplyDemand(Base):
    __tablename__ = "supply_demand"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    owner_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    proposer: Mapped[str] = mapped_column(String(16), nullable=False)
    chain: Mapped[list] = mapped_column(JSONB, nullable=False)
    app: Mapped[str] = mapped_column(String(32), nullable=False)
    task_ref: Mapped[str | None] = mapped_column(String(255))
    confirmation_id: Mapped[str] = mapped_column(String(64), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(128), nullable=False)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    # 0021 起：确认时的供给条件（许可档下限、只用哪几条渠道，0045 第四节）
    conditions: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # 0022 起：最晚什么时候要、说明（0045 第 2.2 节公共部分）
    deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    note: Mapped[str | None] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("app", "idempotency_key", name="uq_supply_demand_key"),
        Index(
            "ix_supply_demand_deadline",
            "deadline",
            postgresql_where=text("deadline IS NOT NULL"),
        ),
        Index("ix_supply_demand_owner", "owner_subject", "created_at"),
    )


class SupplyDemandItem(Base):
    __tablename__ = "supply_demand_item"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    demand_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("supply_demand.id"), nullable=False
    )
    key: Mapped[dict] = mapped_column(JSONB, nullable=False)
    key_text: Mapped[str] = mapped_column(String(200), nullable=False)
    readings: Mapped[list] = mapped_column(JSONB, nullable=False)
    confirmation_item: Mapped[str] = mapped_column(String(16), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    channel_id: Mapped[str | None] = mapped_column(String(40))
    job_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    reason: Mapped[str | None] = mapped_column(String(200))
    receipt: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    # 0021 起：试过、取不到的渠道（换下一个时跳过，0045 第四节）
    tried_channels: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    # 0022 起：给网址的那一项（网址、连不连它直接链到的文件、预填的描述）、再试一次挂在
    # 哪一项下
    url: Mapped[str | None] = mapped_column(Text)
    follow_links: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    prefill: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    retry_of: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    declared_public: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        Index("ix_supply_demand_item_job", "job_id", "status"),
        Index("ix_supply_demand_item_channel_open", "channel_id", "status"),
    )


class SupplyJob(Base):
    __tablename__ = "supply_job"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    channel_id: Mapped[str] = mapped_column(String(40), nullable=False)
    dedup_key: Mapped[str] = mapped_column(String(255), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    tier: Mapped[int] = mapped_column(Integer, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 0021 起：这之前不排（等重试、日上限用完）；调度排出去的时间
    not_before: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 0022 起：派发代次，随投递消息带；只有拿着这一代的 worker 能开跑（二审第 6 条）
    dispatch_token: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued', 'running', 'done', 'failed', 'waiting')",
            name="ck_supply_job_status",
        ),
        Index(
            "uq_supply_job_open",
            "dedup_key",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running', 'waiting')"),
        ),
        Index(
            "ix_supply_job_queue",
            "channel_id",
            "tier",
            "created_at",
            postgresql_where=text("status IN ('queued', 'running', 'waiting')"),
        ),
    )


class SupplyChannelRequest(Base):
    """每问一次渠道一行（0021 起）：限速按它数，只留两天（0045 第 3.1 节）。

    `request` 是问一次（列、查目录），`fetch` 是取回一份（也算问一次）。"""

    __tablename__ = "supply_channel_request"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    channel_id: Mapped[str] = mapped_column(String(40), nullable=False)
    kind: Mapped[str] = mapped_column(String(8), nullable=False)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "kind IN ('request', 'fetch')", name="ck_supply_channel_request_kind"
        ),
        Index("ix_supply_channel_request_channel", "channel_id", "kind", "at"),
        Index("ix_supply_channel_request_at", "at"),
    )


class SupplyTurn(Base):
    """按用户轮流（0021 起）：每条队列里每个用户上次轮到的时间。"""

    __tablename__ = "supply_turn"

    queue: Mapped[str] = mapped_column(String(40), primary_key=True)
    owner_subject: Mapped[str] = mapped_column(String(255), primary_key=True)
    served_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SupplyAttempt(Base):
    __tablename__ = "supply_attempt"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    job_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("supply_job.id"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    outcome: Mapped[str | None] = mapped_column(String(16))
    reason: Mapped[str | None] = mapped_column(String(200))


class SupplyOriginal(Base):
    __tablename__ = "supply_original"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    channel_id: Mapped[str] = mapped_column(String(40), nullable=False)
    channel_doc_id: Mapped[str] = mapped_column(String(200), nullable=False)
    source_url: Mapped[str] = mapped_column(Text, nullable=False)
    media_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    meta: Mapped[dict] = mapped_column(JSONB, nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purge_after: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    purged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        Index(
            "ix_supply_original_purge",
            "purge_after",
            postgresql_where=text("purged_at IS NULL AND purge_after IS NOT NULL"),
        ),
    )


class SupplyOriginalDelivery(Base):
    """原文交付记录（0022 起，三审第 1 条）：按（渠道，文档编号，区，归属）单记，不跨区共用。"""

    __tablename__ = "supply_original_delivery"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    original_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("supply_original.id"), nullable=False
    )
    channel_id: Mapped[str] = mapped_column(String(40), nullable=False)
    channel_doc_id: Mapped[str] = mapped_column(String(200), nullable=False)
    zone: Mapped[str] = mapped_column(String(16), nullable=False)
    owner_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    knowbase_material_id: Mapped[str] = mapped_column(String(64), nullable=False)
    knowbase_version_no: Mapped[int] = mapped_column(Integer, nullable=False)
    readings: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default=text("'[\"original\"]'::jsonb")
    )
    delivered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "zone IN ('disclosure', 'collected')",
            name="ck_supply_original_delivery_zone",
        ),
        UniqueConstraint(
            "channel_id",
            "channel_doc_id",
            "zone",
            "owner_subject",
            name="uq_supply_original_delivery",
        ),
    )


class SupplySubscription(Base):
    """订阅（0022 起，SDD 0045 第 2.2、2.5 节）。"""

    __tablename__ = "supply_subscription"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    owner_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    by: Mapped[str] = mapped_column(String(16), nullable=False)
    subjects: Mapped[list] = mapped_column(JSONB, nullable=False)
    doc_types: Mapped[list] = mapped_column(JSONB, nullable=False)
    urls: Mapped[list] = mapped_column(JSONB, nullable=False)
    topic: Mapped[str | None] = mapped_column(String(200))
    readings: Mapped[list] = mapped_column(JSONB, nullable=False)
    every_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    starts_on: Mapped[date] = mapped_column(Date, nullable=False)
    ends_on: Mapped[date] = mapped_column(Date, nullable=False)
    monthly_cap: Mapped[int] = mapped_column(Integer, nullable=False)
    purpose: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    status_reason: Mapped[str | None] = mapped_column(String(200))
    revision: Mapped[int] = mapped_column(Integer, nullable=False)
    skip_backlog: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    channels: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    round_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        CheckConstraint(
            "by IN ('description', 'url', 'topic')", name="ck_supply_subscription_by"
        ),
        CheckConstraint(
            "status IN ('active', 'paused', 'expired', 'cancelled', 'disabled')",
            name="ck_supply_subscription_status",
        ),
        Index("ix_supply_subscription_owner", "owner_subject", "created_at"),
        Index(
            "ix_supply_subscription_due",
            "next_check_at",
            postgresql_where=text("status IN ('active', 'paused')"),
        ),
    )


class SupplySubscriptionRound(Base):
    """订阅的每一轮：查的时间、发现几份、取了几份、因上限没取几份。"""

    __tablename__ = "supply_subscription_round"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("supply_subscription.id"), nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    found: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    taken: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    over_cap: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    demand_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    note: Mapped[str | None] = mapped_column(String(200))

    __table_args__ = (
        Index(
            "ix_supply_subscription_round_subscription",
            "subscription_id",
            "started_at",
        ),
    )


class SupplySubscriptionTake(Base):
    """各订阅自己的进度：渠道里的哪一份这条订阅取了、本月超额没取、跳过。"""

    __tablename__ = "supply_subscription_take"

    subscription_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("supply_subscription.id"), primary_key=True
    )
    channel_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    channel_doc_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    entry: Mapped[dict] = mapped_column(JSONB, nullable=False)
    target: Mapped[str] = mapped_column(String(400), nullable=False)
    round_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        CheckConstraint(
            "status IN ('taken', 'over_cap', 'skipped')",
            name="ck_supply_subscription_take_status",
        ),
        Index("ix_supply_subscription_take_month", "subscription_id", "status", "at"),
    )


class SupplyDiscovery(Base):
    """渠道里看新的发现的条目（订同一个对象的人共用）。"""

    __tablename__ = "supply_discovery"

    channel_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    target: Mapped[str] = mapped_column(String(400), primary_key=True)
    channel_doc_id: Mapped[str] = mapped_column(String(200), primary_key=True)
    entry: Mapped[dict] = mapped_column(JSONB, nullable=False)
    disclosed_on: Mapped[date | None] = mapped_column(Date)
    found_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SupplyWatchCursor(Base):
    """发现游标：渠道 × 订阅对象，订同一个的人共用。"""

    __tablename__ = "supply_watch_cursor"

    channel_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    target: Mapped[str] = mapped_column(String(400), primary_key=True)
    cursor: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class SupplySearch(Base):
    """按主题找（0022 起）：正在找 → 候选已出（7 天）→ 已结束。"""

    __tablename__ = "supply_search"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    owner_subject: Mapped[str] = mapped_column(String(255), nullable=False)
    topic: Mapped[str] = mapped_column(String(200), nullable=False)
    sites: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    wanted: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    candidates: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    note: Mapped[str | None] = mapped_column(String(200))
    subscription_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        CheckConstraint(
            "status IN ('searching', 'ready', 'ended')", name="ck_supply_search_status"
        ),
        Index("ix_supply_search_owner", "owner_subject", "created_at"),
    )


class SupplyWebSubmission(Base):
    """web端的一次提交（0022 起）：按（用户，幂等钥匙）先占位，再去 knowbase 记确认。"""

    __tablename__ = "supply_web_submission"

    owner_subject: Mapped[str] = mapped_column(String(255), primary_key=True)
    idempotency_key: Mapped[str] = mapped_column(String(128), primary_key=True)
    digest: Mapped[str] = mapped_column(String(64), nullable=False)
    demand_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    result: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
