from app import worker
from core.config import get_settings


def test_configure_celery_binds_default_queue_to_platform_exchange(monkeypatch) -> None:
    monkeypatch.setenv("CELERY_BROKER_URL", "amqp://knowfeed:secret@example/%2Finfo")
    monkeypatch.setenv("CELERY_QUEUE", "knowfeed.admin.default")
    get_settings.cache_clear()
    worker._configured = False

    assert worker.configure_celery(require_broker=True) is True

    conf = worker.celery_app.conf
    assert conf.task_default_queue == "knowfeed.admin.default"
    assert conf.task_default_exchange == "knowfeed.admin.default"
    assert conf.task_default_exchange_type == "direct"
    assert conf.task_default_routing_key == "knowfeed.admin.default"

    queue = next(item for item in conf.task_queues if item.name == "knowfeed.admin.default")
    assert queue.exchange.name == "knowfeed.admin.default"
    assert queue.exchange.type == "direct"
    assert queue.routing_key == "knowfeed.admin.default"
    assert conf.task_routes["app.tasks.*"]["exchange"] == "knowfeed.admin.default"
    assert conf.task_routes["app.tasks.*"]["routing_key"] == "knowfeed.admin.default"

    worker._configured = False
    get_settings.cache_clear()
