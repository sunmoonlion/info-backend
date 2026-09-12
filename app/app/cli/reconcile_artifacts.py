"""Bounded, read-only Info S3/RawArtifact reconciliation. No mutation options."""

from __future__ import annotations

import argparse
import asyncio
import json

from app.application.services.artifact_reconciliation import reconcile_page
from app.infrastructure.storage.object_storage import get_object_storage
from app.infrastructure.storage.postgres import get_postgres


async def run(args):
    postgres = get_postgres()
    storage = get_object_storage()
    client = storage.s3_client()
    try:
        await postgres.init()
        return await asyncio.wait_for(
            reconcile_page(
                postgres.session_factory,
                client,
                bucket=storage.bucket,
                mode=args.mode,
                limit=args.limit,
                cursor=json.loads(args.cursor) if args.cursor else None,
                details=args.include_object_keys,
            ),
            timeout=60,
        )
    finally:
        client.close()
        await postgres.shutdown()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=("inventory", "references"), required=True)
    parser.add_argument("--limit", type=int, choices=range(1, 101), default=50)
    parser.add_argument(
        "--cursor", help="Exact next_cursor JSON from the preceding page"
    )
    parser.add_argument(
        "--include-object-keys",
        action="store_true",
        help="Include filenames/object locators; use only in a private operator terminal",
    )
    args = parser.parse_args()
    try:
        report = asyncio.run(run(args))
    except Exception as exc:
        # Do not leak URLs, credentials, SQL parameters or filenames on failure.
        print(
            json.dumps(
                {
                    "page_complete": False,
                    "deletion_authorized": False,
                    "error_type": type(exc).__name__,
                }
            )
        )
        raise SystemExit(1) from None
    print(json.dumps(report, ensure_ascii=False))
    issues = set(report["counts"]) - {"registered_metadata_match"}
    raise SystemExit(2 if issues else (3 if report["next_cursor"] else 0))


if __name__ == "__main__":
    main()
