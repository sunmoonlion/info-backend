"""Celery producer — admin-backend API 向 RabbitMQ 投递异步任务。"""

from __future__ import annotations

import logging
import uuid
from functools import lru_cache

from celery.result import AsyncResult

from app.worker import celery_app, configure_celery, is_celery_configured
from core.config import get_settings

logger = logging.getLogger(__name__)


class CeleryNotConfiguredError(RuntimeError):
    pass


class CeleryProducer:
    def _ensure_ready(self) -> None:
        if not configure_celery():
            raise CeleryNotConfiguredError(
                "Celery broker not configured (set CELERY_BROKER_URL)"
            )

    @property
    def enabled(self) -> bool:
        if is_celery_configured():
            return True
        return configure_celery()

    def dispatch_ping(self) -> str:
        """投递 ping 任务，返回 Celery task_id。"""
        self._ensure_ready()
        from app.tasks.ping import ping

        queue = get_settings().celery_queue
        async_result = ping.apply_async(queue=queue)
        logger.info("已投递 ping 任务 task_id=%s queue=%s", async_result.id, queue)
        return async_result.id

    def dispatch_crawl_url(self, job_id: uuid.UUID) -> str:
        """投递 URL 采集任务，返回 Celery task_id。"""
        self._ensure_ready()
        from app.tasks.crawl import crawl_url

        queue = get_settings().celery_queue
        async_result = crawl_url.apply_async(args=[str(job_id)], queue=queue)
        logger.info(
            "已投递 crawl_url 任务 task_id=%s job_id=%s queue=%s",
            async_result.id,
            job_id,
            queue,
        )
        return async_result.id

    def dispatch_index_document_version(self, document_version_id: uuid.UUID) -> str:
        """投递 document_version 搜索索引任务，返回 Celery task_id。"""
        self._ensure_ready()
        from app.tasks.search import index_document_version

        queue = get_settings().celery_queue
        async_result = index_document_version.apply_async(
            args=[str(document_version_id)], queue=queue
        )
        logger.info(
            "已投递 index_document_version 任务 task_id=%s document_version_id=%s queue=%s",
            async_result.id,
            document_version_id,
            queue,
        )
        return async_result.id

    def dispatch_distribution(
        self, distribution_id: uuid.UUID, *, outbox_message_id: uuid.UUID
    ) -> str:
        """Publish one durable outbox request with a stable broker task ID."""
        self._ensure_ready()
        from app.tasks.distribution import dispatch_distribution

        queue = get_settings().celery_queue
        async_result = dispatch_distribution.apply_async(
            args=[str(distribution_id), str(outbox_message_id)],
            queue=queue,
            task_id=str(outbox_message_id),
        )
        logger.info(
            "已投递 dispatch_distribution outbox task_id=%s distribution_id=%s "
            "outbox_message_id=%s queue=%s",
            async_result.id,
            distribution_id,
            outbox_message_id,
            queue,
        )
        return async_result.id

    def get_task_result(self, task_id: str) -> AsyncResult:
        self._ensure_ready()
        return AsyncResult(task_id, app=celery_app)


@lru_cache
def get_celery_producer() -> CeleryProducer:
    return CeleryProducer()
