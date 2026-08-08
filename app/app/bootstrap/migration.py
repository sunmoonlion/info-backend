"""Explicit, one-shot Alembic migration process for the Backend image."""

from __future__ import annotations

import argparse
from pathlib import Path

from alembic.config import Config

from alembic import command


def _config() -> Config:
    root = Path(__file__).resolve().parents[2]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "alembic"))
    return config


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the canonical Backend migration chain"
    )
    parser.add_argument(
        "command",
        choices=("upgrade", "current"),
        help="upgrade applies the requested revision; current is read-only",
    )
    parser.add_argument("revision", nargs="?", default="head")
    args = parser.parse_args()
    config = _config()
    if args.command == "upgrade":
        command.upgrade(config, args.revision)
    else:
        if args.revision != "head":
            parser.error("current does not accept a revision")
        command.current(config, verbose=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
