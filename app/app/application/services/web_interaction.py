from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable
from datetime import UTC, datetime
from uuid import UUID

from app.application.dto.interaction import (
    BrowserCitation,
    CitationData,
    CitationEvent,
    DeltaData,
    DeltaEvent,
    InputRequiredData,
    InputRequiredEvent,
    RequiredAction,
    RunAction,
    RunEvent,
    RunSnapshot,
)
from app.application.errors.exceptions import (
    ConcurrencyConflictError,
    ForbiddenError,
    ServiceUnavailableError,
)
from app.application.ports import (
    InteractionContext,
    SourceResolution,
    WebInteractionPort,
)
from core.config import get_settings

REFERENCE_RUN_ID = UUID("00000000-0000-5000-8000-000000000001")
REFERENCE_ACTION_ID = UUID("00000000-0000-5000-8000-000000000020")
REFERENCE_EVIDENCE_ID = UUID("00000000-0000-5000-8000-000000000030")
REFERENCE_EVENT_IDS = tuple(
    UUID(f"00000000-0000-5000-8000-00000000001{index}") for index in range(5)
)


class UnavailableWebInteractionAdapter:
    @staticmethod
    def _unavailable() -> ServiceUnavailableError:
        return ServiceUnavailableError(
            "The interaction provider is not configured",
            code="provider_unavailable",
        )

    async def get_run(self, run_id: str, context: InteractionContext) -> RunSnapshot:
        del run_id, context
        raise self._unavailable()

    async def open_run_stream(
        self,
        run_id: str,
        last_event_id: str | None,
        context: InteractionContext,
    ) -> AsyncIterable[RunEvent]:
        del run_id, last_event_id, context
        raise self._unavailable()

    async def submit_action(
        self,
        run_id: str,
        command: RunAction,
        context: InteractionContext,
    ) -> RunSnapshot:
        del run_id, command, context
        raise self._unavailable()

    async def resolve_citation_source(
        self, evidence_id: str, context: InteractionContext
    ) -> SourceResolution:
        del evidence_id, context
        raise self._unavailable()


class ReferenceWebInteractionAdapter:
    """Deterministic pair-test adapter; production config rejects its use."""

    @staticmethod
    def _authorize(context: InteractionContext) -> None:
        principal = context.principal
        settings = get_settings()
        if (
            principal.actor_type != "user"
            or principal.surface != "web"
            or principal.app != settings.app_slug
        ):
            raise ForbiddenError(
                "The browser identity is not authorized",
                code="resource_forbidden",
            )

    def _authorize_run(self, run_id: str, context: InteractionContext) -> None:
        self._authorize(context)
        if run_id != str(REFERENCE_RUN_ID):
            raise ForbiddenError(
                "The requested run is not available",
                code="resource_forbidden",
            )

    async def get_run(self, run_id: str, context: InteractionContext) -> RunSnapshot:
        self._authorize_run(run_id, context)
        return _initial_snapshot()

    async def open_run_stream(
        self,
        run_id: str,
        last_event_id: str | None,
        context: InteractionContext,
    ) -> AsyncIterable[RunEvent]:
        self._authorize_run(run_id, context)
        cursor = 0
        if last_event_id:
            try:
                cursor = REFERENCE_EVENT_IDS.index(UUID(last_event_id))
            except (ValueError, AttributeError) as exc:
                raise ConcurrencyConflictError() from exc
        events: tuple[RunEvent, ...] = (
            DeltaEvent(
                event_id=REFERENCE_EVENT_IDS[1],
                run_id=REFERENCE_RUN_ID,
                sequence_no=2,
                occurred_at=_timestamp(1),
                type="delta",
                data=DeltaData(text="A streamed FastAPI response fragment. "),
            ),
            CitationEvent(
                event_id=REFERENCE_EVENT_IDS[2],
                run_id=REFERENCE_RUN_ID,
                sequence_no=3,
                occurred_at=_timestamp(2),
                type="citation",
                data=CitationData(citation=_citation()),
            ),
            InputRequiredEvent(
                event_id=REFERENCE_EVENT_IDS[3],
                run_id=REFERENCE_RUN_ID,
                sequence_no=4,
                occurred_at=_timestamp(3),
                type="input_required",
                data=InputRequiredData(
                    action=RequiredAction(
                        action_id=REFERENCE_ACTION_ID,
                        kind="confirmation",
                        prompt="Confirm the reference action.",
                    )
                ),
            ),
        )

        async def generate() -> AsyncIterable[RunEvent]:
            for event in events:
                if event.sequence_no <= cursor + 1:
                    continue
                await asyncio.sleep(0)
                yield event

        return generate()

    async def submit_action(
        self,
        run_id: str,
        command: RunAction,
        context: InteractionContext,
    ) -> RunSnapshot:
        self._authorize_run(run_id, context)
        if command.action_id != REFERENCE_ACTION_ID or command.value != "confirm":
            raise ForbiddenError(
                "The requested action is not allowed", code="action_denied"
            )
        return _initial_snapshot().model_copy(
            update={
                "status": "succeeded",
                "summary": "A streamed FastAPI response fragment.",
                "last_sequence_no": 5,
                "last_event_id": REFERENCE_EVENT_IDS[4],
                "citations": (_citation(),),
                "required_action": None,
                "updated_at": _timestamp(4),
            }
        )

    async def resolve_citation_source(
        self, evidence_id: str, context: InteractionContext
    ) -> SourceResolution:
        self._authorize(context)
        if evidence_id != str(REFERENCE_EVIDENCE_ID):
            raise ForbiddenError(
                "The requested citation is not available",
                code="resource_forbidden",
            )
        return SourceResolution(
            location=f"/api/web/v1/reference/sources/{REFERENCE_EVIDENCE_ID}"
        )


def _timestamp(second: int) -> datetime:
    return datetime(2030, 1, 1, 0, 0, second, tzinfo=UTC)


def _citation() -> BrowserCitation:
    return BrowserCitation(
        evidence_id=REFERENCE_EVIDENCE_ID,
        knowledge_document_id=UUID("00000000-0000-5000-8000-000000000031"),
        knowledge_document_version_id=UUID("00000000-0000-5000-8000-000000000032"),
        chunk_id=UUID("00000000-0000-5000-8000-000000000033"),
        title="FastAPI authorized reference",
        quote="This bounded reference proves the browser citation contract.",
        source_document_id=UUID("00000000-0000-5000-8000-000000000034"),
        source_document_version_id=UUID("00000000-0000-5000-8000-000000000035"),
        content_hash="c" * 64,
        source_href=f"/api/web/v1/citations/{REFERENCE_EVIDENCE_ID}/source",
    )


def _initial_snapshot() -> RunSnapshot:
    return RunSnapshot(
        run_id=REFERENCE_RUN_ID,
        title="FastAPI reference interaction",
        status="running",
        summary=None,
        last_sequence_no=1,
        last_event_id=REFERENCE_EVENT_IDS[0],
        citations=(),
        required_action=None,
        updated_at=_timestamp(0),
    )


async def get_web_interaction_port() -> WebInteractionPort:
    if get_settings().reference_interaction_enabled:
        return ReferenceWebInteractionAdapter()
    return UnavailableWebInteractionAdapter()
