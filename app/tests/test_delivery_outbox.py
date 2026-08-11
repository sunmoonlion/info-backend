from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID

import pytest
from sqlalchemy.dialects import postgresql

from app.application.services import delivery_outbox
from app.application.services.info_crawl_service import create_knowledge_distribution


class _Result:
    def __init__(self, value: Any):
        self._value = value

    def scalar_one_or_none(self) -> Any:
        return self._value

    def scalars(self) -> Any:
        return iter(self._value if isinstance(self._value, list) else [self._value])


class _ClaimSession:
    def __init__(self, messages: Any):
        self.messages = messages
        self.commits = 0
        self.rollbacks = 0
        self.last_query: Any | None = None

    async def execute(self, _query: Any) -> _Result:
        self.last_query = _query
        return _Result(self.messages)

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


def test_distribution_outbox_uses_one_stable_operation_key() -> None:
    distribution_id = UUID("00000000-0000-0000-0000-000000000001")
    first = delivery_outbox.new_distribution_dispatch_outbox(
        distribution_id,
        now=datetime(2026, 7, 14, tzinfo=UTC),
    )
    second = delivery_outbox.new_distribution_dispatch_outbox(
        distribution_id,
        now=datetime(2026, 7, 14, tzinfo=UTC),
    )

    assert first.topic == delivery_outbox.TOPIC_DISTRIBUTION_DISPATCH_V1
    assert first.idempotency_key == second.idempotency_key
    assert first.payload == {"distribution_id": str(distribution_id)}
    assert "credential" not in str(first.payload).lower()


@pytest.mark.asyncio
async def test_redispatch_rearms_the_same_stable_operation() -> None:
    distribution_id = UUID("00000000-0000-0000-0000-000000000001")
    message = delivery_outbox.new_distribution_dispatch_outbox(
        distribution_id,
        now=datetime(2026, 7, 14, tzinfo=UTC),
    )
    message.id = UUID("00000000-0000-0000-0000-000000000010")
    message.state = delivery_outbox.STATE_COMPLETED
    message.attempt_count = 3
    message.lease_token = UUID("00000000-0000-0000-0000-000000000020")
    message.lease_expires_at = datetime(2026, 7, 14, 12, 1, tzinfo=UTC)
    message.published_at = datetime(2026, 7, 14, 12, 2, tzinfo=UTC)
    message.completed_at = datetime(2026, 7, 14, 12, 3, tzinfo=UTC)
    message.last_error = "previous failure"
    original_id = message.id
    original_key = message.idempotency_key
    original_payload = dict(message.payload)
    session = _ClaimSession(message)
    rearmed_at = datetime(2026, 7, 15, tzinfo=UTC)

    rearmed = await delivery_outbox.ensure_distribution_dispatch_outbox(
        cast(Any, session),
        distribution_id=distribution_id,
        now=rearmed_at,
    )

    assert rearmed is message
    assert rearmed.id == original_id
    assert rearmed.idempotency_key == original_key
    assert rearmed.payload == original_payload
    assert rearmed.state == delivery_outbox.STATE_PENDING
    assert rearmed.available_at == rearmed_at
    assert rearmed.attempt_count == 3
    assert rearmed.lease_token is None
    assert rearmed.lease_expires_at is None
    assert rearmed.published_at is None
    assert rearmed.completed_at is None
    assert rearmed.last_error is None


@pytest.mark.asyncio
async def test_claim_uses_one_lease_per_message_and_increments_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    message = delivery_outbox.new_distribution_dispatch_outbox(
        UUID("00000000-0000-0000-0000-000000000001"),
        now=datetime(2026, 7, 14, tzinfo=UTC),
    )
    message.id = UUID("00000000-0000-0000-0000-000000000010")
    message.attempt_count = 0
    session = _ClaimSession([message])
    now = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)

    monkeypatch.setattr(
        delivery_outbox,
        "get_settings",
        lambda: SimpleNamespace(
            delivery_outbox_batch_size=50,
            delivery_outbox_ack_timeout_seconds=300,
            delivery_outbox_lease_seconds=30,
            delivery_outbox_retry_max_seconds=300,
            delivery_outbox_retry_base_seconds=5,
        ),
    )
    claims = await delivery_outbox.claim_due_delivery_outbox(
        cast(Any, session), limit=1, now=now
    )

    assert len(claims) == 1
    assert claims[0].id == message.id
    assert message.state == delivery_outbox.STATE_LEASED
    assert message.lease_token == claims[0].lease_token
    assert message.attempt_count == 1
    assert message.lease_expires_at is not None
    assert message.lease_expires_at > now
    assert session.commits == 1
    assert session.last_query is not None
    compiled = str(session.last_query.compile(dialect=postgresql.dialect()))
    assert "FOR UPDATE SKIP LOCKED" in compiled


@pytest.mark.asyncio
async def test_complete_allows_fast_worker_before_publisher_ack() -> None:
    message = delivery_outbox.new_distribution_dispatch_outbox(UUID(int=1))
    message.id = UUID(int=2)
    message.state = delivery_outbox.STATE_LEASED
    session = _ClaimSession(message)

    completed = await delivery_outbox.complete_delivery_outbox(
        cast(Any, session), message_id=message.id
    )

    assert completed is True
    assert message.state == delivery_outbox.STATE_COMPLETED
    assert message.completed_at is not None
    assert session.commits == 1


def test_retry_delay_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        delivery_outbox,
        "get_settings",
        lambda: SimpleNamespace(
            delivery_outbox_retry_max_seconds=300,
            delivery_outbox_retry_base_seconds=5,
        ),
    )
    assert delivery_outbox.retry_delay_seconds(1) == 5
    assert delivery_outbox.retry_delay_seconds(2) == 10
    assert delivery_outbox.retry_delay_seconds(99) == 300


@pytest.mark.asyncio
async def test_dispatch_batch_records_broker_failure_for_scanner_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claim = delivery_outbox.LeasedOutboxMessage(
        id=UUID(int=1),
        distribution_id=UUID(int=2),
        lease_token=UUID(int=3),
        attempt_count=1,
    )
    released: list[dict[str, Any]] = []

    async def fake_claim(*_args: Any, **_kwargs: Any) -> list[delivery_outbox.LeasedOutboxMessage]:
        return [claim]

    async def fake_release(*_args: Any, **kwargs: Any) -> bool:
        released.append(kwargs)
        return True

    monkeypatch.setattr(delivery_outbox, "claim_due_delivery_outbox", fake_claim)
    monkeypatch.setattr(delivery_outbox, "release_delivery_outbox", fake_release)

    class BrokenPublisher:
        def dispatch_distribution(
            self, distribution_id: UUID, *, outbox_message_id: UUID
        ) -> str:
            assert distribution_id == claim.distribution_id
            assert outbox_message_id == claim.id
            raise RuntimeError("rabbitmq unavailable")

    summary = await delivery_outbox.dispatch_due_delivery_outbox(
        cast(Any, object()), publisher=BrokenPublisher()
    )

    assert summary.claimed == 1
    assert summary.published == 0
    assert summary.broker_failures == 1
    assert released[0]["message_id"] == claim.id
    assert released[0]["error"] == "broker_publish_failed:RuntimeError"


class _CreateDistributionSession:
    def __init__(self, values: dict[tuple[type[Any], UUID], Any]):
        self.values = values
        self.events: list[str] = []

    async def get(self, model: type[Any], identifier: UUID) -> Any:
        return self.values[(model, identifier)]

    def add(self, _value: Any) -> None:
        self.events.append("add")

    async def flush(self) -> None:
        self.events.append("flush")

    async def commit(self) -> None:
        self.events.append("commit")

    async def refresh(self, _value: Any) -> None:
        self.events.append("refresh")


@pytest.mark.asyncio
async def test_requested_distribution_writes_outbox_before_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.application.services import info_crawl_service
    from app.infrastructure.models.info import (
        InfoDocument,
        InfoDocumentVersion,
        RawArtifact,
    )

    document_id = UUID(int=10)
    version_id = UUID(int=11)
    artifact_id = UUID(int=12)
    session = _CreateDistributionSession(
        {
            (InfoDocumentVersion, version_id): SimpleNamespace(
                id=version_id,
                document_id=document_id,
                content_hash="b" * 64,
                clean_artifact_id=artifact_id,
                text_artifact_id=None,
                title="Example",
                source_url="https://example.com/a",
            ),
            (InfoDocument, document_id): SimpleNamespace(
                id=document_id,
                source_name="Example",
                published_at=None,
                metadata_json={},
            ),
            (RawArtifact, artifact_id): SimpleNamespace(
                document_version_id=version_id,
                storage_state="available",
                version_id="s3-version-1",
                artifact_type="clean_markdown",
                size_bytes=10,
                sha256="a" * 64,
                content_type="text/markdown",
                bucket="bucket",
                object_key="info/original/a.md",
            ),
        }
    )

    async def fake_ensure(_session: Any, *, distribution_id: UUID) -> Any:
        assert distribution_id
        assert session.events == ["add", "flush"]
        session.events.append("outbox")
        return SimpleNamespace()

    monkeypatch.setattr(info_crawl_service, "ensure_distribution_dispatch_outbox", fake_ensure)
    await create_knowledge_distribution(
        cast(Any, session), document_version_id=version_id, dispatch=True
    )

    assert session.events == ["add", "flush", "outbox", "commit", "refresh"]


@pytest.mark.asyncio
async def test_create_only_distribution_does_not_silently_enqueue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.application.services import info_crawl_service
    from app.infrastructure.models.info import (
        InfoDocument,
        InfoDocumentVersion,
        RawArtifact,
    )

    document_id = UUID(int=20)
    version_id = UUID(int=21)
    artifact_id = UUID(int=22)
    session = _CreateDistributionSession(
        {
            (InfoDocumentVersion, version_id): SimpleNamespace(
                id=version_id,
                document_id=document_id,
                content_hash="b" * 64,
                clean_artifact_id=artifact_id,
                text_artifact_id=None,
                title="Example",
                source_url="https://example.com/a",
            ),
            (InfoDocument, document_id): SimpleNamespace(
                id=document_id,
                source_name="Example",
                published_at=None,
                metadata_json={},
            ),
            (RawArtifact, artifact_id): SimpleNamespace(
                document_version_id=version_id,
                storage_state="available",
                version_id="s3-version-1",
                artifact_type="clean_markdown",
                size_bytes=10,
                sha256="a" * 64,
                content_type="text/markdown",
                bucket="bucket",
                object_key="info/original/a.md",
            ),
        }
    )

    async def fail_if_called(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("create-only distribution must not enqueue")

    monkeypatch.setattr(info_crawl_service, "ensure_distribution_dispatch_outbox", fail_if_called)
    await create_knowledge_distribution(
        cast(Any, session), document_version_id=version_id, dispatch=False
    )

    assert session.events == ["add", "commit", "refresh"]


@pytest.mark.asyncio
async def test_best_effort_kick_uses_isolated_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.interfaces.endpoints import info_routes

    dispatch_session = object()
    dispatched_with: list[Any] = []

    class _DispatchSessionContext:
        async def __aenter__(self) -> Any:
            return dispatch_session

        async def __aexit__(self, *_args: Any) -> None:
            return None

    class _DispatchSessionFactory:
        def __call__(self) -> _DispatchSessionContext:
            return _DispatchSessionContext()

    async def fake_dispatch(session: Any, *, publisher: Any) -> Any:
        dispatched_with.append((session, publisher))
        return SimpleNamespace()

    producer = SimpleNamespace(enabled=True)
    monkeypatch.setattr(info_routes, "get_celery_producer", lambda: producer)
    monkeypatch.setattr(
        info_routes,
        "get_postgres",
        lambda: SimpleNamespace(session_factory=_DispatchSessionFactory()),
    )
    monkeypatch.setattr(info_routes, "dispatch_due_delivery_outbox", fake_dispatch)

    await info_routes._best_effort_kick_delivery_outbox()

    assert dispatched_with == [(dispatch_session, producer)]


class _WorkerSessionContext:
    async def __aenter__(self) -> Any:
        return SimpleNamespace()

    async def __aexit__(self, *_args: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_worker_completes_only_after_business_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks import distribution as distribution_task

    completed: list[UUID] = []
    released: list[dict[str, Any]] = []

    class FakePostgres:
        session_factory = _WorkerSessionContext

        def __init__(self) -> None:
            self.shutdown_called = False

        async def init(self) -> None:
            return None

        async def shutdown(self) -> None:
            self.shutdown_called = True

    class ExpiringRecord:
        def __init__(self) -> None:
            self.expired = False

        @property
        def id(self) -> UUID:
            if self.expired:
                raise AssertionError("worker read record.id after commit")
            return UUID(int=30)

        @property
        def status(self) -> str:
            if self.expired:
                raise AssertionError("worker read record.status after commit")
            return "succeeded"

    record = ExpiringRecord()

    async def successful_dispatch(*_args: Any, **_kwargs: Any) -> Any:
        return record

    async def fake_complete(*_args: Any, **kwargs: Any) -> bool:
        completed.append(kwargs["message_id"])
        record.expired = True
        return True

    async def fake_release(*_args: Any, **kwargs: Any) -> bool:
        released.append(kwargs)
        return True

    postgres = FakePostgres()
    monkeypatch.setattr(distribution_task, "get_postgres", lambda: postgres)
    monkeypatch.setattr(distribution_task, "dispatch_distribution_service", successful_dispatch)
    monkeypatch.setattr(distribution_task, "complete_delivery_outbox", fake_complete)
    monkeypatch.setattr(distribution_task, "release_delivery_outbox", fake_release)

    result = await distribution_task._run(UUID(int=30), UUID(int=31))

    assert result == str(UUID(int=30))
    assert completed == [UUID(int=31)]
    assert released == []
    assert postgres.shutdown_called is True
