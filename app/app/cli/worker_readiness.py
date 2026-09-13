"""Bounded, local-node Celery consumer configuration readiness (not progress)."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from collections.abc import Callable
from typing import Any

PROBE_TIMEOUT_SECONDS = 6.0
REPLY_TIMEOUT_SECONDS = 1.0


def _reply(broadcast: Callable[..., Any], command: str, node: str) -> list:
    replies = broadcast(
        command, destination=[node], reply=True, timeout=REPLY_TIMEOUT_SECONDS
    )
    # Do not flatten/grep replies: wrong nodes and malformed/error replies fail closed.
    if not isinstance(replies, list) or len(replies) != 1:
        raise ValueError("invalid reply")
    item = replies[0]
    if not isinstance(item, dict) or set(item) != {node}:
        raise ValueError("invalid node")
    payload = item[node]
    if not isinstance(payload, list):
        raise ValueError("invalid payload")
    return payload


def check_worker(app: Any, node: str) -> bool:
    """Query only this node; no ping task, DB access, or consumer mutation."""
    queue = app.conf.task_default_queue
    if not isinstance(queue, str) or not queue:
        return False
    required = {name for name in app.tasks if name.startswith("app.tasks.")}
    if not required:
        return False
    queues = _reply(app.control.broadcast, "active_queues", node)
    if len(queues) != 1 or not isinstance(queues[0], dict):
        return False
    actual = queues[0]
    exchange = actual.get("exchange")
    if not isinstance(exchange, dict):
        return False
    # Current four-role backend config owns exactly one durable direct queue.
    if not (
        actual.get("name") == queue
        and actual.get("routing_key") == app.conf.task_default_routing_key
        and actual.get("durable") is True
        and actual.get("auto_delete") is False
        and actual.get("exclusive") is False
        and exchange.get("name") == app.conf.task_default_exchange
        and exchange.get("type") == app.conf.task_default_exchange_type
        and exchange.get("durable") is True
        and exchange.get("auto_delete") is False
    ):
        return False
    registered = _reply(app.control.broadcast, "registered", node)
    return all(isinstance(name, str) for name in registered) and required.issubset(
        registered
    )


def _check_local() -> bool:
    pod = os.environ.get("POD_NAME", "")
    if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", pod):
        return False
    # Import/configuration errors remain inside the bounded, silenced child.
    from app.bootstrap.worker import celery_app

    return check_worker(celery_app, f"celery@{pod}")


def _bounded(command: list[str], timeout: float) -> bool:
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=False,
        )
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        # subprocess.run kills and waits for its child on timeout.
        return False


def main() -> None:
    if sys.argv[1:] == ["--check-local"]:
        try:
            ready = _check_local()
        except Exception:
            ready = False
        raise SystemExit(0 if ready else 1)
    if sys.argv[1:]:
        print("worker_not_ready", file=sys.stderr)
        raise SystemExit(1)
    ready = _bounded(
        [sys.executable, "-m", "app.cli.worker_readiness", "--check-local"],
        PROBE_TIMEOUT_SECONDS,
    )
    if not ready:
        print("worker_not_ready", file=sys.stderr)
    raise SystemExit(0 if ready else 1)


if __name__ == "__main__":
    main()
