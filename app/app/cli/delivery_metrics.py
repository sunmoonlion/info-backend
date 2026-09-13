"""Print a read-only delivery snapshot; no claim, publish, reconcile, replay or GC."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.infrastructure.messaging.delivery_observation import (
    collect_delivery_snapshot,
    render_prometheus,
)
from app.infrastructure.messaging.delivery_observers import get_delivery_observers
from app.infrastructure.storage.postgres import get_postgres


async def run() -> dict:
    postgres = get_postgres()
    await postgres.init()
    try:
        sessions = postgres.session_factory
        return await collect_delivery_snapshot(
            sessions, get_delivery_observers(sessions)
        )
    finally:
        await postgres.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--format", choices=["json", "prometheus"], default="json")
    args = parser.parse_args()
    try:
        snapshot = asyncio.run(run())
        output = (
            render_prometheus(snapshot)
            if args.format == "prometheus"
            else json.dumps(snapshot) + "\n"
        )
    except Exception:
        # Never print driver exceptions, SQL parameters, malformed headers or DSNs.
        print("delivery_observation_failed", file=sys.stderr)
        raise SystemExit(2) from None
    print(output, end="")


if __name__ == "__main__":
    main()
