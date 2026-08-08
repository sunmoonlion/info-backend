from __future__ import annotations

import json
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class OutboxEvent(BaseModel):
    """Transport-neutral event persisted with the owning business transaction."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    topic: str = Field(pattern=r"^[a-z0-9][a-z0-9._-]{0,127}$")
    aggregate_key: str = Field(min_length=1, max_length=512)
    deduplication_key: str = Field(min_length=1, max_length=512)
    payload: dict[str, Any]
    headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("headers")
    @classmethod
    def validate_headers(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 32 or any(
            not key
            or len(key) > 128
            or len(item) > 2048
            or "\n" in key
            or "\r" in key
            or "\n" in item
            or "\r" in item
            for key, item in value.items()
        ):
            raise ValueError("outbox headers exceed the transport contract")
        return value

    @model_validator(mode="after")
    def validate_serialized_size(self) -> OutboxEvent:
        encoded = json.dumps(
            {"payload": self.payload, "headers": self.headers},
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode()
        if len(encoded) > 262_144:
            raise ValueError("outbox event exceeds 256 KiB")
        return self


class ClaimedOutboxEvent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: UUID
    topic: str
    aggregate_key: str
    payload: dict[str, Any]
    headers: dict[str, str]
    attempt_count: int
