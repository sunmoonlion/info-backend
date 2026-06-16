from app.worker import celery_app


@celery_app.task(name="app.tasks.ping")
def ping() -> str:
    """Health-check task for worker / Flower."""
    return "pong"
