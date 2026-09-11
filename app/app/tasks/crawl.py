"""Retired direct task: old messages must not bypass durable execution."""

from app.worker import celery_app


@celery_app.task(name="app.tasks.crawl_url")
def crawl_url(job_id: str):
    raise RuntimeError("direct task retired; submit through the application outbox")
