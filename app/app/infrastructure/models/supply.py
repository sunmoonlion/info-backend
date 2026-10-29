"""供给的表（SDD 0045 第五节）：渠道与审核、试采记录，需求单与需求项、作业与尝试、留 30 天
的原件。"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (
        UniqueConstraint("app", "idempotency_key", name="uq_supply_demand_key"),
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
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    __table_args__ = (Index("ix_supply_demand_item_job", "job_id", "status"),)


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
    knowbase_material_id: Mapped[str | None] = mapped_column(String(64))
    knowbase_version_no: Mapped[int | None] = mapped_column(Integer)

    __table_args__ = (
        Index(
            "ix_supply_original_purge",
            "purge_after",
            postgresql_where=text("purged_at IS NULL AND purge_after IS NOT NULL"),
        ),
    )
