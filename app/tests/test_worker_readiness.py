from __future__ import annotations

import copy
import os
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from app.cli import worker_readiness as probe

NODE = "celery@test-pod"
TASK = "app.tasks.durable_delivery.execute"


@pytest.fixture
def configured():
    queue = {
        "name": "test.default",
        "routing_key": "test.default",
        "durable": True,
        "auto_delete": False,
        "exclusive": False,
        "exchange": {
            "name": "test.default",
            "type": "direct",
            "durable": True,
            "auto_delete": False,
        },
    }
    replies = {"active_queues": [{NODE: [queue]}], "registered": [{NODE: [TASK]}]}
    broadcast = Mock(side_effect=lambda command, **kwargs: replies[command])
    app = SimpleNamespace(
        conf=SimpleNamespace(
            task_default_queue="test.default",
            task_default_exchange="test.default",
            task_default_exchange_type="direct",
            task_default_routing_key="test.default",
        ),
        tasks={TASK: object(), "celery.backend_cleanup": object()},
        control=SimpleNamespace(broadcast=broadcast),
    )
    return app, replies


def test_exact_local_queue_and_real_task_registry(configured):
    app, replies = configured
    assert probe.check_worker(app, NODE)
    assert [call.args for call in app.control.broadcast.call_args_list] == [
        ("active_queues",),
        ("registered",),
    ]
    for call in app.control.broadcast.call_args_list:
        assert call.kwargs == {"destination": [NODE], "reply": True, "timeout": 1.0}
    app.tasks["app.tasks.domain.extension"] = object()
    assert not probe.check_worker(app, NODE)
    replies["registered"][0][NODE].append("app.tasks.domain.extension")
    assert probe.check_worker(app, NODE)


@pytest.mark.parametrize(
    "field",
    [
        "name",
        "routing_key",
        "durable",
        "auto_delete",
        "exclusive",
        "exchange.name",
        "exchange.type",
        "exchange.durable",
        "exchange.auto_delete",
    ],
)
def test_each_queue_mismatch_fails(configured, field):
    app, replies = configured
    item = replies["active_queues"][0][NODE][0]
    keys = field.split(".")
    if len(keys) == 2:
        item = item[keys[0]]
    item[keys[-1]] = "wrong"
    assert not probe.check_worker(app, NODE)


@pytest.mark.parametrize("payload", [[], [{"name": "test.default"}], [None]])
def test_empty_incomplete_queue_fails(configured, payload):
    app, replies = configured
    replies["active_queues"] = [{NODE: payload}]
    assert not probe.check_worker(app, NODE)


def test_extra_queue_fails(configured):
    app, replies = configured
    queues = replies["active_queues"][0][NODE]
    queues.append(copy.deepcopy(queues[0]))
    assert not probe.check_worker(app, NODE)


@pytest.mark.parametrize(
    "reply",
    [
        None,
        {},
        [],
        [{"another-node": []}],
        [{NODE: {"error": "secret"}}],
        [{NODE: []}, {NODE: []}],
        [{NODE: [], "another-node": []}],
    ],
)
def test_malformed_or_wrong_node_reply_rejected(configured, reply):
    app, replies = configured
    replies["active_queues"] = reply
    with pytest.raises(ValueError):
        probe.check_worker(app, NODE)


@pytest.mark.parametrize("registered", [[], ["other"], [TASK, {}], [TASK, None]])
def test_missing_or_invalid_registered_tasks(configured, registered):
    app, replies = configured
    replies["registered"] = [{NODE: registered}]
    assert not probe.check_worker(app, NODE)


def test_missing_local_registration_is_not_vacuous_success(configured):
    app, _ = configured
    app.tasks = {}
    assert not probe.check_worker(app, NODE)
    app.conf.task_default_queue = ""
    assert not probe.check_worker(app, NODE)
    app.control.broadcast.assert_not_called()


@pytest.mark.parametrize("pod", ["", "../pod", "celery@other", "-bad", "a" * 254])
def test_missing_or_invalid_pod_fails_without_broker(monkeypatch, pod):
    monkeypatch.setenv("POD_NAME", pod)
    assert not probe._check_local()


def test_internal_failure_and_outer_output_are_generic(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["probe", "--check-local"])
    monkeypatch.setattr(probe, "_check_local", Mock(side_effect=ValueError("secret")))
    with pytest.raises(SystemExit) as result:
        probe.main()
    assert result.value.code == 1
    assert capsys.readouterr() == ("", "")
    monkeypatch.setattr(sys, "argv", ["probe"])
    bounded = Mock(return_value=False)
    monkeypatch.setattr(probe, "_bounded", bounded)
    with pytest.raises(SystemExit) as result:
        probe.main()
    assert result.value.code == 1
    bounded.assert_called_once_with(
        [sys.executable, "-m", "app.cli.worker_readiness", "--check-local"], 6.0
    )
    assert capsys.readouterr() == ("", "worker_not_ready\n")


def test_outer_success_is_silent(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["probe"])
    monkeypatch.setattr(probe, "_bounded", Mock(return_value=True))
    with pytest.raises(SystemExit) as result:
        probe.main()
    assert result.value.code == 0
    assert capsys.readouterr() == ("", "")


def test_real_timeout_kills_and_reaps_its_child(monkeypatch):
    processes = []
    popen = subprocess.Popen

    def capture(*args, **kwargs):
        child = popen(*args, **kwargs)
        processes.append(child)
        return child

    monkeypatch.setattr(subprocess, "Popen", capture)
    started = time.monotonic()
    assert not probe._bounded(
        [sys.executable, "-c", "import time; time.sleep(30)"], 0.2
    )
    assert time.monotonic() - started < 3
    assert len(processes) == 1
    assert processes[0].returncode is not None
    with pytest.raises(ProcessLookupError):
        os.kill(processes[0].pid, 0)


def test_child_output_and_start_failure_hidden(capsys):
    assert not probe._bounded([sys.executable, "-c", "raise RuntimeError('secret')"], 3)
    assert not probe._bounded(["/nonexistent/luna-worker-probe"], 1)
    assert capsys.readouterr() == ("", "")
