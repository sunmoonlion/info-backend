from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BrowserCitation(ContractModel):
    contract_version: Literal[1] = 1
    evidence_id: UUID
    knowledge_document_id: UUID
    knowledge_document_version_id: UUID
    chunk_id: UUID
    title: str | None = Field(max_length=4096)
    quote: str = Field(min_length=1, max_length=1000)
    source_document_id: UUID
    source_document_version_id: UUID
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    source_href: str = Field(
        min_length=1,
        max_length=160,
        pattern=r"^/api/web/v1/citations/[0-9a-fA-F-]{36}/source$",
    )

    @model_validator(mode="after")
    def source_matches_evidence(self) -> BrowserCitation:
        expected = f"/api/web/v1/citations/{self.evidence_id}/source"
        if self.source_href.lower() != expected.lower():
            raise ValueError("source_href must identify evidence_id")
        return self


class RequiredAction(ContractModel):
    action_id: UUID
    kind: Literal["confirmation", "input"]
    prompt: str = Field(min_length=1, max_length=2000)


RunStatus = Literal[
    "queued",
    "running",
    "waiting_for_input",
    "succeeded",
    "failed",
    "cancelled",
]


class RunSnapshot(ContractModel):
    contract_version: Literal[1] = 1
    run_id: UUID
    title: str = Field(min_length=1, max_length=512)
    status: RunStatus
    summary: str | None = Field(default=None, max_length=20000)
    last_sequence_no: int = Field(ge=0)
    last_event_id: UUID | None
    citations: tuple[BrowserCitation, ...] = Field(max_length=50)
    required_action: RequiredAction | None
    updated_at: datetime

    @field_validator("updated_at")
    @classmethod
    def timestamp_has_offset(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("updated_at must contain a UTC offset")
        return value

    @field_validator("citations")
    @classmethod
    def citations_are_unique(
        cls, value: tuple[BrowserCitation, ...]
    ) -> tuple[BrowserCitation, ...]:
        evidence_ids = [item.evidence_id for item in value]
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("citation evidence_id values must be unique")
        return value


class EventBase(ContractModel):
    contract_version: Literal[1] = 1
    event_id: UUID
    run_id: UUID
    sequence_no: int = Field(ge=1)
    occurred_at: datetime

    @field_validator("occurred_at")
    @classmethod
    def timestamp_has_offset(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must contain a UTC offset")
        return value


class StatusData(ContractModel):
    status: RunStatus


class StatusEvent(EventBase):
    type: Literal["status"]
    data: StatusData


class DeltaData(ContractModel):
    text: str = Field(min_length=1, max_length=4096)


class DeltaEvent(EventBase):
    type: Literal["delta"]
    data: DeltaData


class CitationData(ContractModel):
    citation: BrowserCitation


class CitationEvent(EventBase):
    type: Literal["citation"]
    data: CitationData


class InputRequiredData(ContractModel):
    action: RequiredAction


class InputRequiredEvent(EventBase):
    type: Literal["input_required"]
    data: InputRequiredData


class CompletedData(ContractModel):
    summary: str = Field(max_length=20000)


class CompletedEvent(EventBase):
    type: Literal["completed"]
    data: CompletedData


class FailedData(ContractModel):
    code: str = Field(min_length=1, max_length=128)
    message: str = Field(min_length=1, max_length=1000)


class FailedEvent(EventBase):
    type: Literal["failed"]
    data: FailedData


class HeartbeatData(ContractModel):
    pass


class HeartbeatEvent(EventBase):
    type: Literal["heartbeat"]
    data: HeartbeatData


RunEvent = Annotated[
    StatusEvent
    | DeltaEvent
    | CitationEvent
    | InputRequiredEvent
    | CompletedEvent
    | FailedEvent
    | HeartbeatEvent,
    Field(discriminator="type"),
]
RUN_EVENT_ADAPTER = TypeAdapter(RunEvent)


class RunAction(ContractModel):
    contract_version: Literal[1] = 1
    action_id: UUID
    value: str = Field(max_length=4000)
