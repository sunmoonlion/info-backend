"""Read local Beat activity, never restart it or query broker/database credentials."""

import argparse
import json
import sys

from app.infrastructure.messaging.scheduler_activity import read_activity


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schedule", required=True)
    parser.add_argument("--max-age", type=float, required=True)
    args = parser.parse_args()
    try:
        snapshot = read_activity(args.schedule, max_age=args.max_age)
    except Exception:
        print("scheduler_activity_unavailable", file=sys.stderr)
        raise SystemExit(1) from None
    print(json.dumps(snapshot))


if __name__ == "__main__":
    main()
