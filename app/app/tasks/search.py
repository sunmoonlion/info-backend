"""Retired direct task: old messages must not bypass durable execution."""

from app.worker import celery_app


@celery_app.task(name="app.tasks.index_document_version")
def index_document_version(document_version_id: str):
    raise RuntimeError("direct task retired; submit through the application outbox")
