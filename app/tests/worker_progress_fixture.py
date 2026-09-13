"""Test assembly: shared Celery transport, isolated DB and synthetic handler."""

import os
import re

from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.bootstrap.worker import celery_app as celery_app
from app.infrastructure.storage.postgres import Postgres
from app.tasks import durable_delivery

TOPIC = "test.counter.v1"
url = make_url(os.environ["DELIVERY_TEST_DATABASE_URL"])
schema = os.environ["DELIVERY_PROGRESS_TEST_SCHEMA"]
if (
    url.host not in {"localhost", "127.0.0.1"}
    or not url.database
    or not url.database.endswith("_tests")
    or not re.fullmatch(r"delivery_test_[0-9a-f]{32}", schema)
):
    raise RuntimeError("requires isolated loopback test database/schema")


class TestPostgres(Postgres):
    async def init(self):
        self._engine = create_async_engine(
            url.set(drivername="postgresql+asyncpg"),
            connect_args={"server_settings": {"search_path": schema + ",public"}},
            hide_parameters=True,
        )
        self._session_factory = async_sessionmaker(self._engine, expire_on_commit=False)


async def increment(session, payload):
    await session.execute(
        text("UPDATE delivery_test_counter SET value=value+1 WHERE id=1")
    )
    if payload.get("fail"):
        raise RuntimeError("synthetic-handler-rollback")


# Only this test entry point substitutes storage and the public handler extension.
# The registered task, consume/lease/Inbox implementation remain production code.
durable_delivery.get_postgres = TestPostgres
durable_delivery.get_delivery_handlers = lambda: {TOPIC: increment}
