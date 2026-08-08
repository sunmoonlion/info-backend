from __future__ import annotations

from collections.abc import AsyncIterable
from dataclasses import dataclass
from typing import Protocol

from app.application.dto.interaction import RunAction, RunEvent, RunSnapshot
from app.domain.security import Principal


@dataclass(frozen=True)
class InteractionContext:
    principal: Principal
    operation_id: str


@dataclass(frozen=True)
class SourceResolution:
    location: str


class WebInteractionPort(Protocol):
    async def get_run(
        self, run_id: str, context: InteractionContext
    ) -> RunSnapshot: ...

    async def open_run_stream(
        self,
        run_id: str,
        last_event_id: str | None,
        context: InteractionContext,
    ) -> AsyncIterable[RunEvent]: ...

    async def submit_action(
        self,
        run_id: str,
        command: RunAction,
        context: InteractionContext,
    ) -> RunSnapshot: ...

    async def resolve_citation_source(
        self,
        evidence_id: str,
        context: InteractionContext,
    ) -> SourceResolution: ...
