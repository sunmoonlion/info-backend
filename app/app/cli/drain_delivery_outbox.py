"""Compatibility CLI: use the shared dispatcher for all registered Info topics."""

import argparse
import asyncio
import json

from app.tasks.durable_delivery import _run


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--limit",
        type=int,
        default=100,
        help="retained for old CronJobs; shared dispatcher uses bounded batches of 100",
    )
    args = parser.parse_args()
    if args.limit != 100:
        parser.error("the shared dispatcher supports --limit=100 only")
    print(json.dumps({"claimed": asyncio.run(_run(None))}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
