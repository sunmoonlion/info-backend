"""Local activity is not broker confirmation, Worker readiness or business progress."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest
from celery import Celery
from celery.beat import PersistentScheduler, ScheduleEntry, SchedulingError

from app.cli import scheduler_activity as cli
from app.infrastructure.messaging import scheduler_activity as activity


@pytest.fixture
def record(tmp_path):
    schedule = str(tmp_path / "beat")
    _, start = activity.process_identity(os.getpid())
    data = {
        "schema_version": 1,
        "pid": os.getpid(),
        "process_start": start,
        "boot_id": activity.boot_id(),
        "tick_boottime": activity.boottime(),
        "tick_count": 1,
        "publish_returns": 0,
        "publish_errors": 0,
    }
    activity.atomic_write(activity.activity_path(schedule), data)
    return schedule, data


def test_atomic_private_snapshot_and_known_projection(record):
    schedule, data = record
    path = activity.activity_path(schedule)
    assert path.stat().st_mode & 0o777 == 0o600
    output = activity.read_activity(schedule, max_age=30)
    assert output["tick_count"] == 1 and output["publish_returns"] == 0
    assert 0 <= output["loop_age_seconds"] <= 30
    assert "healthy" not in output and "boot_id" not in output
    assert "process_start" not in output
    data["private_extra"] = "must-not-be-projected"
    activity.atomic_write(path, data)
    assert "must-not-be-projected" not in json.dumps(
        activity.read_activity(schedule, max_age=30)
    )
    assert list(path.parent.glob(".beat-activity-*")) == []


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("pid", True),
        ("pid", 0),
        ("tick_count", 0),
        ("tick_count", -1),
        ("publish_returns", "1"),
        ("publish_errors", -1),
        ("tick_boottime", -1),
        ("tick_boottime", float("nan")),
        ("tick_boottime", float("inf")),
        ("tick_boottime", True),
        ("process_start", "wrong"),
        ("boot_id", "other-boot"),
    ],
)
def test_invalid_or_replaced_process_state_is_rejected(record, field, value):
    schedule, data = record
    data[field] = value
    # Malformed-input fixture deliberately includes JSON NaN/Infinity.
    activity.activity_path(schedule).write_text(json.dumps(data))
    with pytest.raises(ValueError):
        activity.read_activity(schedule, max_age=30)


@pytest.mark.parametrize("state", ["T", "t", "Z", "X", "unknown"])
def test_stopped_or_dead_process_is_rejected(record, monkeypatch, state):
    schedule, data = record
    monkeypatch.setattr(
        activity, "process_identity", lambda pid: (state, data["process_start"])
    )
    with pytest.raises(ValueError):
        activity.read_activity(schedule, max_age=30)


def test_age_is_boottime_not_wall_clock_or_file_mtime(record, monkeypatch):
    schedule, data = record
    monkeypatch.setattr(activity.time, "time", lambda: -(10**10))
    os.utime(activity.activity_path(schedule), (0, 0))
    assert activity.read_activity(schedule, max_age=30)["tick_count"] == 1
    monkeypatch.setattr(activity, "boottime", lambda: data["tick_boottime"] + 31)
    with pytest.raises(ValueError, match="stale"):
        activity.read_activity(schedule, max_age=30)
    monkeypatch.setattr(activity, "boottime", lambda: data["tick_boottime"] - 1)
    with pytest.raises(ValueError, match="stale"):
        activity.read_activity(schedule, max_age=30)


@pytest.mark.parametrize("budget", [0, -1, 3601, float("nan"), float("inf")])
def test_invalid_age_budget_is_rejected_before_open(tmp_path, budget):
    with pytest.raises(ValueError):
        activity.read_activity(str(tmp_path / "missing"), max_age=budget)


@pytest.mark.parametrize(
    "fault", ["missing", "large", "json", "list", "symlink", "fifo", "owner"]
)
def test_untrusted_files_fail_closed(record, monkeypatch, fault):
    schedule, _ = record
    path = activity.activity_path(schedule)
    if fault == "missing":
        path.unlink()
    elif fault == "large":
        path.write_text(" " * (activity.MAX_BYTES + 1))
    elif fault == "json":
        path.write_text("not-json")
    elif fault == "list":
        path.write_text("[]")
    elif fault == "symlink":
        other = path.with_suffix(".target")
        path.rename(other)
        path.symlink_to(other)
    elif fault == "fifo":
        path.unlink()
        os.mkfifo(path)
    else:
        monkeypatch.setattr(activity.os, "getuid", lambda: -1)
    with pytest.raises((OSError, ValueError)):
        activity.read_activity(schedule, max_age=30)


def test_failed_atomic_replace_preserves_old_snapshot_and_cleans_temp(
    record, monkeypatch
):
    schedule, data = record
    path = activity.activity_path(schedule)
    before = path.read_bytes()
    monkeypatch.setattr(
        activity.os, "replace", Mock(side_effect=OSError("private-path"))
    )
    data["tick_count"] = 2
    with pytest.raises(OSError):
        activity.atomic_write(path, data)
    assert path.read_bytes() == before
    assert list(path.parent.glob(".beat-activity-*")) == []


def scheduler(tmp_path):
    app = Celery("activity-unit", broker="memory://")
    app.conf.beat_schedule = {"existing": {"task": "existing.task", "schedule": 5}}
    return activity.ObservedScheduler(
        app=app,
        schedule_filename=str(tmp_path / "schedule"),
        max_interval=5,
    )


def test_tick_inherits_algorithm_rate_limits_writes_and_failure_is_not_activity(
    tmp_path, monkeypatch
):
    instance = scheduler(tmp_path)
    try:
        assert instance.schedule["existing"].task == "existing.task"
        inherited = Mock(return_value=4.99)
        monkeypatch.setattr(PersistentScheduler, "tick", inherited)
        clock = [100.0]
        monkeypatch.setattr(activity, "boottime", lambda: clock[0])
        monkeypatch.setattr(activity.time, "monotonic", lambda: clock[0])
        assert instance.tick() == 4.99
        path = activity.activity_path(instance.schedule_filename)
        first = path.read_bytes()
        assert instance.tick() == 4.99
        assert path.read_bytes() == first
        clock[0] += activity.WRITE_INTERVAL_SECONDS
        instance.tick()
        assert json.loads(path.read_bytes())["tick_count"] == 3
        previous = path.read_bytes()
        inherited.side_effect = RuntimeError("failed-tick")
        with pytest.raises(RuntimeError):
            instance.tick()
        assert path.read_bytes() == previous
    finally:
        instance.close()


def test_publish_failure_is_separate_from_completed_tick(tmp_path, monkeypatch):
    instance = scheduler(tmp_path)
    try:
        entry = instance.schedule["existing"]
        sent = Mock(return_value=object())
        monkeypatch.setattr(PersistentScheduler, "apply_async", sent)
        assert instance.apply_async(entry, advance=False) is sent.return_value
        sent.assert_called_once_with(entry, advance=False)
        sent.side_effect = SchedulingError("synthetic-error")
        with pytest.raises(SchedulingError):
            instance.apply_async(entry)
        monkeypatch.setattr(PersistentScheduler, "tick", lambda self: 5)
        instance.tick()
        output = activity.read_activity(instance.schedule_filename, max_age=30)
        assert output["tick_count"] == 1
        assert output["publish_returns"] == output["publish_errors"] == 1
    finally:
        instance.close()


def test_real_celery_apply_async_and_persistent_schedule_are_preserved(tmp_path):
    instance = scheduler(tmp_path)
    try:
        entry = ScheduleEntry("one", task="app.test", schedule=1, app=instance.app)
        result = instance.apply_async(entry)
        assert result.id
        assert instance.schedule["one"].total_run_count == 1
        assert instance._publish_returns == 1
        instance.sync()
    finally:
        instance.close()
    restored = scheduler(tmp_path)
    try:
        assert restored.schedule["existing"].task == "existing.task"
    finally:
        restored.close()


def test_real_celery_publish_exception_is_not_a_publish_return(tmp_path, monkeypatch):
    instance = scheduler(tmp_path)
    try:
        monkeypatch.setattr(
            instance, "send_task", Mock(side_effect=RuntimeError("synthetic-broker"))
        )
        entry = instance.schedule["existing"]
        with pytest.raises(SchedulingError):
            instance.apply_async(entry)
        assert instance._publish_errors == 1
        assert instance._publish_returns == 0
        # Celery advances a due entry even on publish failure; observer preserves it.
        assert instance.schedule["existing"].total_run_count == 1
    finally:
        instance.close()


def test_unsupported_clock_is_rate_limited_without_aborting_scheduler(
    tmp_path, monkeypatch, caplog
):
    instance = scheduler(tmp_path)
    try:
        monkeypatch.setattr(PersistentScheduler, "tick", lambda self: 0)
        monkeypatch.setattr(activity.time, "monotonic", lambda: 100.0)
        monkeypatch.setattr(
            activity, "boottime", Mock(side_effect=AttributeError("clock-unavailable"))
        )
        for _ in range(10):
            assert instance.tick() == 0
        assert caplog.text.count("scheduler_activity_write_failed") == 1
        assert not activity.activity_path(instance.schedule_filename).exists()
    finally:
        instance.close()


def test_write_failure_does_not_stop_scheduling_or_log_private_exception(
    tmp_path, monkeypatch, caplog
):
    instance = scheduler(tmp_path)
    try:
        monkeypatch.setattr(PersistentScheduler, "tick", lambda self: 5)
        monkeypatch.setattr(
            activity, "atomic_write", Mock(side_effect=OSError("secret://private"))
        )
        assert instance.tick() == 5
        assert "scheduler_activity_write_failed" in caplog.text
        assert "secret://private" not in caplog.text
        assert not activity.activity_path(instance.schedule_filename).exists()
    finally:
        instance.close()


def test_cli_projects_success_and_failure_does_not_leak_paths(
    record, monkeypatch, capsys
):
    schedule, _ = record
    monkeypatch.setattr(
        sys, "argv", ["activity", "--schedule", schedule, "--max-age", "30"]
    )
    cli.main()
    assert json.loads(capsys.readouterr().out)["tick_count"] == 1
    monkeypatch.setattr(
        cli, "read_activity", Mock(side_effect=ValueError("private://path"))
    )
    with pytest.raises(SystemExit) as result:
        cli.main()
    assert result.value.code == 1
    output = capsys.readouterr()
    assert output.out == "" and output.err == "scheduler_activity_unavailable\n"


def test_scheduler_bootstrap_selects_observer_without_changing_worker_schedule():
    root = Path(__file__).resolve().parents[1]
    bootstrap = (root / "app/bootstrap/scheduler.py").read_text()
    assert (
        "app.infrastructure.messaging.scheduler_activity:ObservedScheduler" in bootstrap
    )
