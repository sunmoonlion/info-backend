from fastapi import APIRouter, HTTPException

from app.infrastructure.messaging.celery_producer import (
    CeleryNotConfiguredError,
    get_celery_producer,
)
from core.config import get_settings

router = APIRouter(prefix="/internal/tasks", tags=["内部-异步任务"])


@router.post("/ping", summary="投递 Celery ping 任务（联调/健康检查）")
async def enqueue_ping():
    producer = get_celery_producer()
    if not producer.enabled:
        raise HTTPException(status_code=503, detail="Celery producer 未配置")
    try:
        task_id = producer.dispatch_ping()
    except CeleryNotConfiguredError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e
    return {"task_id": task_id, "queue": get_settings().celery_queue}
