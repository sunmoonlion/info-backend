"""Run one bounded durable-delivery scanner pass.

This command is designed for a Kubernetes CronJob.  It owns no long-running
loop: every invocation claims a finite batch with SKIP LOCKED and exits, so a
stalled process can be replaced safely.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from app.application.services.delivery_outbox import dispatch_due_delivery_outbox
from app.infrastructure.messaging.celery_producer import get_celery_producer
from app.infrastructure.storage.postgres import get_postgres


async def _run(limit: int) -> dict[str, int]:
    producer = get_celery_producer()
    if not producer.enabled:
        raise RuntimeError("CELERY_BROKER_URL is required to drain delivery outbox")

    postgres = get_postgres()
    await postgres.init()
    try:
        async with postgres.session_factory() as session:
            summary = await dispatch_due_delivery_outbox(
                session, publisher=producer, limit=limit
            )
            return {
                "claimed": summary.claimed,
                "published": summary.published,
                "broker_failures": summary.broker_failures,
            }
    finally:
        await postgres.shutdown()


def main() -> int:
    parser = argparse.ArgumentParser(description="drain Info delivery outbox once")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    if args.limit < 1 or args.limit > 1000:
        parser.error("--limit must be between 1 and 1000")

    logging.basicConfig(level=logging.INFO)
    result = asyncio.run(_run(args.limit))
    print(json.dumps({"task": "info-delivery-outbox", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - command entry point
    raise SystemExit(main())
