from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Principal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    actor_type: Literal["user", "service"]
    subject: str = Field(min_length=1, max_length=512)
    issuer: str = Field(min_length=1, max_length=2048)
    app: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    surface: Literal["admin", "web", "internal"]
    audience: str = Field(min_length=1, max_length=512)
    actor_id: UUID | None = None
    display_name: str | None = Field(default=None, max_length=256)
    email: str | None = Field(default=None, max_length=320)
    roles: tuple[str, ...] = ()
    scopes: frozenset[str] = frozenset()
    authenticated_at: datetime
    expires_at: datetime
    policy_version: str = Field(min_length=1, max_length=64)

    @field_validator("roles")
    @classmethod
    def normalize_roles(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(sorted({item.strip() for item in value if item.strip()}))
        if len(normalized) > 128 or any(len(item) > 128 for item in normalized):
            raise ValueError("roles exceed the browser session contract")
        return normalized

    @field_validator("scopes")
    @classmethod
    def normalize_scopes(cls, value: frozenset[str]) -> frozenset[str]:
        normalized = frozenset(item.strip() for item in value if item.strip())
        if len(normalized) > 128 or any(len(item) > 128 for item in normalized):
            raise ValueError("scopes exceed the browser session contract")
        return normalized

    @field_validator("authenticated_at", "expires_at")
    @classmethod
    def require_timezone(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("security timestamps must be timezone-aware")
        return value

    @model_validator(mode="after")
    def validate_actor(self) -> Principal:
        if self.actor_type == "user" and self.actor_id is None:
            raise ValueError("user principal requires actor_id")
        if self.expires_at <= self.authenticated_at:
            raise ValueError("principal expiration must follow authentication")
        return self

    def has_scopes(self, required: set[str] | frozenset[str]) -> bool:
        return required.issubset(self.scopes)


class BrowserSession(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    contract_version: Literal[1] = 1
    principal: Principal
    csrf_token: str = Field(min_length=32, max_length=256)

    @model_validator(mode="after")
    def require_browser_principal(self) -> BrowserSession:
        if self.principal.actor_type != "user" or self.principal.surface not in {
            "admin",
            "web",
        }:
            raise ValueError("browser session requires a browser user principal")
        return self


class LoginStart(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    authorization_url: str
    transaction_id: str


class SessionStart(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    session_id: str
    expires_at: datetime
