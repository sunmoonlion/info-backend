"""Compatibility path only. The code lives in infrastructure since 2026-09-29.

Deployment integration scripts still import from here, and they are frozen while
the cluster migrates. Delete this file together with those imports afterwards.
New code imports `app.infrastructure.messaging.durable_tasks` from an outer layer.
"""

from app.infrastructure.messaging.durable_tasks import (
    CHECK_LEASE,
    ConsumerLease,
    DurableTasks,
    Handler,
    assert_execution_current,
    enqueue_task,
)

__all__ = [
    "CHECK_LEASE",
    "ConsumerLease",
    "DurableTasks",
    "Handler",
    "assert_execution_current",
    "enqueue_task",
]
