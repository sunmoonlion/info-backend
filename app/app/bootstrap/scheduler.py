"""Celery Beat bootstrap for the same immutable Backend image."""

from app.worker import celery_app, configure_celery

configure_celery(require_broker=True)
celery_app.conf.beat_scheduler = (
    "app.infrastructure.messaging.scheduler_activity:ObservedScheduler"
)

__all__ = ["celery_app"]
