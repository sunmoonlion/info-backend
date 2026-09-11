"""Retired direct task: old messages must not bypass durable execution."""

from app.worker import celery_app


@celery_app.task(name="app.tasks.dispatch_distribution")
def dispatch_distribution(distribution_id: str, outbox_message_id: str):
    raise RuntimeError("direct task retired; submit through the application outbox")
