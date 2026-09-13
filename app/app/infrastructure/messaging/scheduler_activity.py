"""Linux-local, disposable Beat activity. Not a delivery ledger or health verdict."""

from __future__ import annotations

import json
import logging
import math
import os
import stat
import tempfile
import time
from pathlib import Path

from celery.beat import PersistentScheduler

logger = logging.getLogger(__name__)
MAX_BYTES = 4096
WRITE_INTERVAL_SECONDS = 1.0


def boottime() -> float:
    # Includes Linux suspend time, unlike wall clock, and resets across boots.
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def process_identity(pid: int) -> tuple[str, str]:
    content = Path(f"/proc/{pid}/stat").read_text()
    fields = content[content.rindex(")") + 2 :].split()
    # fields[0] is stat field 3 (state); field 22 is process start ticks.
    return fields[0], fields[19]


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def activity_path(schedule: str) -> Path:
    return Path(schedule + ".activity.json")


def atomic_write(path: Path, snapshot: dict) -> None:
    # Same-directory replace, private file, no partial JSON visible to readers.
    fd, temporary = tempfile.mkstemp(prefix=".beat-activity-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(snapshot, output, allow_nan=False)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_activity(schedule: str, *, max_age: float) -> dict:
    """Only for the same trusted UID/PID namespace; not protection from that UID."""
    if not math.isfinite(max_age) or not 0 < max_age <= 3600:
        raise ValueError("invalid activity age budget")
    fd = os.open(activity_path(schedule), os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as source:
        metadata = os.fstat(source.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
            raise ValueError("invalid activity file")
        raw = source.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("activity file too large")
    snapshot = json.loads(raw)
    if (
        not isinstance(snapshot, dict)
        or type(snapshot.get("schema_version")) is not int
        or snapshot["schema_version"] != 1
    ):
        raise ValueError("invalid activity schema")
    for key in ("pid", "tick_count", "publish_returns", "publish_errors"):
        if type(snapshot.get(key)) is not int or snapshot[key] < 0:
            raise ValueError("invalid activity counter")
    if snapshot["pid"] == 0 or snapshot["tick_count"] == 0:
        raise ValueError("no completed scheduler tick")
    tick_at = snapshot.get("tick_boottime")
    if isinstance(tick_at, bool) or not isinstance(tick_at, (float, int)):
        raise ValueError("invalid tick time")
    if not math.isfinite(tick_at) or tick_at < 0:
        raise ValueError("invalid tick time")
    age = boottime() - tick_at
    if not 0 <= age <= max_age or snapshot.get("boot_id") != boot_id():
        raise ValueError("stale activity")
    state, start = process_identity(snapshot["pid"])
    if state not in {"R", "S", "D", "I"} or start != snapshot.get("process_start"):
        raise ValueError("inactive or replaced scheduler")
    # Project only known fields; caller never gets arbitrary file contents.
    return {
        "schema_version": 1,
        "pid": snapshot["pid"],
        "loop_age_seconds": age,
        "tick_count": snapshot["tick_count"],
        "publish_returns": snapshot["publish_returns"],
        "publish_errors": snapshot["publish_errors"],
    }


class ObservedScheduler(PersistentScheduler):
    """Keep Celery scheduling intact; observe completed ticks, not timer threads."""

    def __init__(self, *args, **kwargs):
        self._tick_count = 0
        self._publish_returns = 0
        self._publish_errors = 0
        self._last_write_attempt = float("-inf")
        super().__init__(*args, **kwargs)

    def apply_async(self, *args, **kwargs):
        try:
            result = super().apply_async(*args, **kwargs)
        except Exception:
            self._publish_errors += 1
            raise
        self._publish_returns += 1
        return result

    def tick(self, *args, **kwargs):
        delay = super().tick(*args, **kwargs)
        self._tick_count += 1
        try:
            attempt_at = time.monotonic()
            if attempt_at - self._last_write_attempt < WRITE_INTERVAL_SECONDS:
                return delay
            self._last_write_attempt = attempt_at
            now = boottime()
            pid = os.getpid()
            _, start = process_identity(pid)
            filename = self.schedule_filename
            if not isinstance(filename, str):
                raise ValueError("activity requires a schedule filename")
            atomic_write(
                activity_path(filename),
                {
                    "schema_version": 1,
                    "pid": pid,
                    "process_start": start,
                    "boot_id": boot_id(),
                    "tick_boottime": now,
                    "tick_count": self._tick_count,
                    "publish_returns": self._publish_returns,
                    "publish_errors": self._publish_errors,
                },
            )
        except Exception:
            # Observation failure must not abort scheduling or leak paths/credentials.
            logger.warning("scheduler_activity_write_failed")
        return delay
