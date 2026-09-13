"""Existing signed metrics route exposes committed receipts without mutation."""

from types import SimpleNamespace

from test_delivery_metrics_http import PATH, client, endpoint
from test_delivery_metrics_http import identity as identity
from test_delivery_observation import dump
from test_durable_delivery_db import TOPIC, request, runtime, sql
from test_durable_delivery_db import db as db


async def test_signed_receipt_scrape_and_invalid_timestamp_fail_closed(
    db, monkeypatch, identity
):
    identifier = await request(db)
    assert await runtime(db).consume(identifier)
    monkeypatch.setattr(
        endpoint, "get_postgres", lambda: SimpleNamespace(session_factory=db)
    )
    monkeypatch.setattr(
        endpoint,
        "get_delivery_observers",
        lambda sessions: {"tasks": runtime(sessions)},
    )
    before = await dump(db)
    async with client() as http:
        response = await http.get(PATH, headers=identity())
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-store"
        assert (
            "sunmoonai_delivery_retained_receipt_messages"
            f'{{policy="tasks",topic="{TOPIC}"}} 1' in response.text
        )
        assert await dump(db) == before
        await sql(db, "UPDATE inbox_message SET processed_at='infinity'")
        failed = await http.get(PATH, headers=identity())
        assert failed.status_code == 503
        assert failed.headers["Cache-Control"] == "no-store"
        assert "sunmoonai_delivery_" not in failed.text
        assert "infinity" not in failed.text
        await sql(db, "UPDATE inbox_message SET processed_at=clock_timestamp()")
        assert (await http.get(PATH, headers=identity())).status_code == 200
