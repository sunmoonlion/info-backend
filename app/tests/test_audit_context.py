from __future__ import annotations

import uuid

from starlette.datastructures import Headers

from app.application.audit_context import from_headers


def test_headers_are_read_case_insensitively() -> None:
    context = from_headers(
        Headers(
            {
                "x-correlation-id": "corr-1",
                "X-OPERATION-ID": "op:1",
                "x-audit-reason": "rotate key",
            }
        )
    )
    assert (context.correlation_id, context.operation_id, context.reason) == (
        "corr-1",
        "op:1",
        "rotate key",
    )


def test_unsafe_values_are_dropped_and_a_correlation_id_is_always_present() -> None:
    context = from_headers(
        Headers({"x-correlation-id": "bad id", "x-audit-reason": "line\nbreak"})
    )
    assert uuid.UUID(context.correlation_id)
    assert context.operation_id is None and context.reason is None
    assert uuid.UUID(from_headers(Headers({})).correlation_id)


def test_the_old_import_path_of_durable_tasks_still_resolves() -> None:
    """Deployment integration scripts import it; remove with them, not before."""
    import app.application.services.durable_tasks as old
    import app.infrastructure.messaging.durable_tasks as new

    for name in old.__all__:
        assert getattr(old, name) is getattr(new, name)
