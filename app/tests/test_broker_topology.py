"""Predeclared topology is explicit; pidbox/events are not disabled."""

from unittest.mock import Mock

import pytest
from celery import Celery
from celery.exceptions import QueueNotFound
from pydantic import ValidationError

from app import worker
from core import config


@pytest.mark.parametrize("predeclared", [False, True])
def test_topology_mode_preserves_contract(monkeypatch, predeclared):
    settings = config.Settings(
        _env_file=None,
        CELERY_BROKER_URL="memory://",
        CELERY_QUEUE="synthetic.tasks",
        CELERY_TASK_TOPOLOGY_PREDECLARED=predeclared,
    )
    app = Celery("topology-test")
    monkeypatch.setattr(config, "get_settings", lambda: settings)
    monkeypatch.setattr(worker, "celery_app", app)
    monkeypatch.setattr(worker, "_configured", False)
    assert worker.configure_celery(require_broker=True)
    queue = app.amqp.queues[settings.celery_queue]
    assert queue.no_declare is predeclared
    assert queue.durable and queue.exchange.durable
    assert queue.name == queue.exchange.name == queue.routing_key
    assert queue.exchange.type == "direct"
    assert app.conf.worker_enable_remote_control
    if predeclared:
        assert app.conf.broker_transport_options == {"confirm_publish": True}
        for task in ("app.tasks.ping", "celery.backend_cleanup"):
            route = app.amqp.router.route({}, task)
            assert route["mandatory"] is True
            assert route["confirm_timeout"] == 5.0
        channel = Mock()
        queue.bind(channel).declare()
        assert channel.mock_calls == []
        with pytest.raises(QueueNotFound):
            app.send_task("app.tasks.ping", queue="unprovisioned")
    else:
        assert app.conf.broker_transport_options == {}
        assert app.amqp.queues["unprovisioned"].name == "unprovisioned"


def test_invalid_topology_mode_fails_closed():
    with pytest.raises(ValidationError):
        config.Settings(_env_file=None, CELERY_TASK_TOPOLOGY_PREDECLARED="typo")
