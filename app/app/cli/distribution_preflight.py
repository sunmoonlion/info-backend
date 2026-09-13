"""Audit logical delivery conflicts; never merge/delete rows or run migrations."""

from __future__ import annotations

import asyncio
import json

from sqlalchemy import text

from app.infrastructure.repositories.distribution_identity_v1 import (
    audit_distribution_identity_v1,
)
from app.infrastructure.storage.postgres import get_postgres


async def audit_database(sessions) -> dict:
    async with sessions() as session, session.begin():
        await session.execute(
            text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
        )
        connection = await session.connection()
        report = await connection.run_sync(audit_distribution_identity_v1)
        report["read_only"] = (
            await session.scalar(text("SHOW transaction_read_only")) == "on"
        )
        return report


async def run() -> dict:
    postgres = get_postgres()
    await postgres.init()
    try:
        return await audit_database(postgres.session_factory)
    finally:
        await postgres.shutdown()


def main():
    report = asyncio.run(run())
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(0 if report["ready"] else 2)


if __name__ == "__main__":
    main()
