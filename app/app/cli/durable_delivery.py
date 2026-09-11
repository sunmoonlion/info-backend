"""Inspect/replay durable delivery without deleting consumer acknowledgements."""

from __future__ import annotations

import argparse
import asyncio
import json
import uuid

from sqlalchemy import text

from app.application.services.durable_tasks import DurableTasks
from app.infrastructure.messaging.delivery_handlers import get_delivery_handlers
from app.infrastructure.storage.postgres import get_postgres


async def run(args):
    postgres = get_postgres()
    await postgres.init()
    try:
        delivery = DurableTasks(
            postgres.session_factory, handlers=get_delivery_handlers()
        )
        if args.command == "reconcile":
            return {"requeued": await delivery.reconcile()}
        if args.command == "replay":
            if not args.message_id:
                raise ValueError("--message-id is required for replay")
            await delivery.replay(uuid.UUID(args.message_id))
            return {"replayed": args.message_id}
        async with postgres.session_factory() as session:
            rows = await session.execute(
                text("""
                SELECT m.id,m.topic,m.aggregate_key,m.attempt_count,
                    d.error_code,d.failed_at,d.replayed_at
                FROM outbox_dead_letter d JOIN outbox_message m ON m.id=d.message_id
                WHERE m.topic=ANY(:topics) ORDER BY d.failed_at DESC LIMIT 100
            """),
                {"topics": list(delivery.topics)},
            )
            return [dict(row) for row in rows.mappings()]
    finally:
        await postgres.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["dead-letters", "reconcile", "replay"])
    parser.add_argument("--message-id")
    print(json.dumps(asyncio.run(run(parser.parse_args())), default=str))


if __name__ == "__main__":
    main()
