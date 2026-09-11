"""KIND probe using a synthetic handler at the real extension point."""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
import uuid

from sqlalchemy import text

from app.application.services.durable_tasks import enqueue_task
from app.infrastructure.storage.postgres import get_postgres

TOPIC = "gate.increment.v1"


async def handler(session, payload):
    await session.execute(text("UPDATE delivery_probe SET value=value+1 WHERE id=1"))


async def setup():
    postgres = get_postgres()
    await postgres.init()
    try:
        async with postgres.session_factory() as session, session.begin():
            await session.execute(
                text(
                    "CREATE TABLE IF NOT EXISTS delivery_probe"
                    "(id int PRIMARY KEY,value int)"
                )
            )
            await session.execute(
                text("INSERT INTO delivery_probe VALUES (1,0) ON CONFLICT DO NOTHING")
            )
            return str(
                await enqueue_task(
                    session,
                    topic=TOPIC,
                    key="one",
                    payload={},
                    deduplication_key="gate-one",
                )
            )
    finally:
        await postgres.shutdown()


async def verify(message_id):
    postgres = get_postgres()
    await postgres.init()
    try:
        async with postgres.session_factory() as session:
            value = (
                await session.execute(
                    text("SELECT value FROM delivery_probe WHERE id=1")
                )
            ).scalar_one()
            count = (
                await session.execute(
                    text("SELECT count(*) FROM inbox_message WHERE message_id=:id"),
                    {"id": uuid.UUID(message_id)},
                )
            ).scalar_one()
            return value == 1 and count == 1
    finally:
        await postgres.shutdown()


def main():
    if os.environ.get("ENV") != "test":
        raise RuntimeError("probe is restricted to disposable test environments")
    if sys.argv[1] in {"worker", "beat"}:
        import app.infrastructure.messaging.delivery_handlers as extension

        extension.get_delivery_handlers = lambda: {TOPIC: handler}
        from app.worker import celery_app, configure_celery

        configure_celery(require_broker=True)
        if sys.argv[1] == "worker":
            celery_app.worker_main(
                ["worker", "--pool=solo", "--concurrency=1", "--loglevel=WARNING"]
            )
        else:
            celery_app.start(
                ["beat", "--schedule=/tmp/probe-beat", "--loglevel=WARNING"]
            )
        return
    subprocess.run(
        [sys.executable, "-m", "app.bootstrap.migration", "upgrade", "head"], check=True
    )
    message_id = asyncio.run(setup())
    children = [
        subprocess.Popen([sys.executable, __file__, role])
        for role in ["worker", "beat"]
    ]
    try:
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            if asyncio.run(verify(message_id)):
                break
            if any(child.poll() is not None for child in children):
                raise RuntimeError("runtime process exited")
            time.sleep(1)
        else:
            raise RuntimeError(
                "Scheduler/Worker failed to acknowledge persisted command"
            )
        from app.tasks.durable_delivery import execute
        from app.worker import configure_celery

        configure_celery(require_broker=True)
        execute.apply_async(args=[message_id])
        time.sleep(3)
        assert asyncio.run(verify(message_id)), "duplicate changed committed result"
        print("KIND_DELIVERY_PROBE_PASSED", flush=True)
    finally:
        for child in children:
            child.terminate()
        for child in children:
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    main()
