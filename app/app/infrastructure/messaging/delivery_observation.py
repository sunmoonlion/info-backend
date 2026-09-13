"""Read-only gauges derived from the actual delivery policies, never a new ledger."""

from __future__ import annotations

import asyncio
import math
import re
from collections.abc import Mapping
from datetime import datetime

from sqlalchemy import text

from app.application.dto.outbox import NOT_BEFORE_HEADER
from app.infrastructure.messaging.durable_delivery import DurableDelivery

OBSERVATION_TIMEOUT_SECONDS = 2.0
GAUGES = {
    "messages": "Retained outbox messages, not a lifetime counter.",
    "retained_receipt_messages": (
        "Retained messages with a committed receipt for the required consumer."
    ),
    "latest_receipt_recorded_timestamp_seconds": (
        "Maximum retained receipt processed_at, not commit time or a heartbeat; "
        "zero without matching receipts."
    ),
    "incomplete_messages": "Messages without the policy's required completion.",
    "scheduled_messages": "Incomplete messages before immutable not-before.",
    "publishable_messages": "Due incomplete messages without dead letter or execution.",
    "awaiting_receipt_messages": "Published messages without required receipt.",
    "active_execution_messages": "Incomplete messages with this policy's active lease.",
    "expired_publisher_leases": "Incomplete delivering messages with expired lease.",
    "dead_letter_messages": "Messages with an unreplayed dead-letter record.",
    "reconcile_candidates": "Timed-out unacknowledged publish without execution/DLQ.",
    "oldest_due_incomplete_age_seconds": (
        "Age since creation or not-before of oldest due incomplete non-DLQ message, "
        "including active work."
    ),
}


async def collect_delivery_snapshot(
    sessions, policies: Mapping[str, DurableDelivery]
) -> dict:
    """Policies/SQL come only from trusted code; callers cannot submit SQL or topics."""
    topics: set[str] = set()
    if len(policies) > 16:
        raise ValueError("too many delivery policies")
    for name, policy in policies.items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", name):
            raise ValueError("invalid delivery policy name")
        for topic in policy.topics:
            if (
                not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", topic)
                or topic in topics
            ):
                raise ValueError("invalid or overlapping delivery topic")
            topics.add(topic)
    if len(topics) > 128:
        raise ValueError("too many delivery topics")
    async with asyncio.timeout(OBSERVATION_TIMEOUT_SECONDS):
        async with sessions() as session, session.begin():
            await session.execute(
                text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            )
            await session.execute(text("SET LOCAL statement_timeout = '1500ms'"))
            observed_at = await session.scalar(text("SELECT clock_timestamp()"))
            unregistered = await session.scalar(
                text(
                    "SELECT count(*) FROM outbox_message WHERE NOT (topic=ANY(:topics))"
                ),
                {"topics": sorted(topics)},
            )
            observations = []
            for name, policy in sorted(policies.items()):
                for topic, consumer in sorted(policy.topics.items()):
                    # active_execution_sql is the existing policy extension point,
                    # including AgentDelivery's domain lease store, not copied logic.
                    row = (
                        (
                            await session.execute(
                                text(f"""
            WITH facts AS (
                SELECT m.status,m.created_at,m.available_at,
                    m.lease_expires_at,m.published_at,
                    coalesce((m.headers->>'{NOT_BEFORE_HEADER}')::timestamptz,
                        '-infinity'::timestamptz) AS due_at,
                    i.processed_at AS receipt_at,
                    CASE WHEN CAST(:consumer AS text) IS NULL
                        THEN m.status='published'
                        ELSE i.message_id IS NOT NULL
                        END AS done,
                    EXISTS ({policy.active_execution_sql}) AS executing,
                    EXISTS (SELECT 1 FROM outbox_dead_letter d
                        WHERE d.message_id=m.id AND d.replayed_at IS NULL) AS dead
                FROM outbox_message m LEFT JOIN inbox_message i
                    ON i.message_id=m.id AND i.consumer=CAST(:consumer AS text)
                WHERE m.topic=:topic
            )
            SELECT count(*) AS messages,
                coalesce(bool_or(NOT isfinite(receipt_at)),false)
                    AS invalid_receipt_time,
                count(receipt_at) AS retained_receipt_messages,
                coalesce(extract(epoch FROM max(receipt_at)),0)::float8
                    AS latest_receipt_recorded_timestamp_seconds,
                count(*) FILTER (WHERE NOT done) AS incomplete_messages,
                count(*) FILTER (WHERE NOT done AND due_at>:now)
                    AS scheduled_messages,
                count(*) FILTER (WHERE NOT done AND NOT dead AND NOT executing
                    AND due_at<=:now AND available_at<=:now
                    AND (status='pending' OR
                        (status='delivering' AND lease_expires_at<=:now)))
                    AS publishable_messages,
                count(*) FILTER (WHERE NOT done AND status='published'
                    AND CAST(:consumer AS text) IS NOT NULL)
                    AS awaiting_receipt_messages,
                count(*) FILTER (WHERE NOT done AND executing)
                    AS active_execution_messages,
                count(*) FILTER (WHERE NOT done AND status='delivering'
                    AND lease_expires_at<=:now) AS expired_publisher_leases,
                count(*) FILTER (WHERE dead) AS dead_letter_messages,
                count(*) FILTER (WHERE NOT done AND NOT executing AND NOT dead
                    AND CAST(:consumer AS text) IS NOT NULL AND status='published'
                    AND published_at<:now-(:ttl * interval '1 second'))
                    AS reconcile_candidates,
                coalesce(max(greatest(0,
                    extract(epoch FROM (:now-greatest(created_at,due_at)))))
                    FILTER (WHERE NOT done AND NOT dead AND due_at<=:now),0)::float8
                    AS oldest_due_incomplete_age_seconds
            FROM facts
                    """),
                                {
                                    "consumer": consumer,
                                    "topic": topic,
                                    "now": observed_at,
                                    "ttl": policy.lease_seconds,
                                },
                            )
                        )
                        .mappings()
                        .one()
                    )
                    values = dict(row)
                    invalid_time = values.pop("invalid_receipt_time")
                    if invalid_time or not math.isfinite(
                        values["latest_receipt_recorded_timestamp_seconds"]
                    ):
                        raise ValueError("invalid receipt timestamp")
                    observations.append(
                        {
                            "policy": name,
                            "topic": topic,
                            "receipt_required": consumer is not None,
                            **values,
                        }
                    )
            return {
                "schema_version": 1,
                "observed_at": observed_at.isoformat(),
                "unregistered_messages": unregistered,
                "topics": observations,
            }


def render_prometheus(snapshot: dict) -> str:
    """Text 0.0.4, static bounded labels, complete metric families, gauges only."""
    lines = [
        "# HELP sunmoonai_delivery_snapshot_timestamp_seconds Snapshot start time.",
        "# TYPE sunmoonai_delivery_snapshot_timestamp_seconds gauge",
        "sunmoonai_delivery_snapshot_timestamp_seconds "
        f"{datetime.fromisoformat(snapshot['observed_at']).timestamp()}",
        "# HELP sunmoonai_delivery_unregistered_messages "
        "Retained messages outside configured observation policies.",
        "# TYPE sunmoonai_delivery_unregistered_messages gauge",
        f"sunmoonai_delivery_unregistered_messages {snapshot['unregistered_messages']}",
    ]
    for key, help_text in GAUGES.items():
        metric = "sunmoonai_delivery_" + key
        lines.extend([f"# HELP {metric} {help_text}", f"# TYPE {metric} gauge"])
        for row in snapshot["topics"]:
            # Labels were validated before collection and never originate in DB rows.
            labels = f'policy="{row["policy"]}",topic="{row["topic"]}"'
            lines.append(f"{metric}{{{labels}}} {row[key]}")
    return "\n".join(lines) + "\n"
