import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.infrastructure.logging.logging import setup_logging
from app.infrastructure.messaging.celery_producer import get_celery_producer
from app.infrastructure.storage.postgres import get_postgres
from app.infrastructure.storage.redis import get_redis
from app.interfaces.endpoints.routes import router
from app.interfaces.errors.exception_handlers import register_exception_handlers
from core.config import get_settings

settings = get_settings()
setup_logging()
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger.info("应用正在启动...")

    # 初始化基础设施
    await get_redis().init()
    await get_postgres().init()

    producer = get_celery_producer()
    if producer.enabled:
        logger.info(
            "Celery producer 已启用，queue=%s",
            settings.celery_queue,
        )
    else:
        logger.info("Celery producer 未配置，跳过")

    try:
        yield
    finally:
        logger.info("应用正在关闭...")
        await get_redis().shutdown()
        await get_postgres().shutdown()
        logger.info("应用已关闭")


app = FastAPI(
    title="info Admin Backend",
    description="通用后台管理 API 服务",
    version="0.1.0",
    lifespan=lifespan,
    docs_url=None if settings.env == "production" else "/docs",
    redoc_url=None if settings.env == "production" else "/redoc",
    openapi_url=None if settings.env == "production" else "/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(settings.frontend_origin_list),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Accept", "Content-Type", "X-CSRF-Token", "X-Correlation-ID"],
    expose_headers=["X-Correlation-ID"],
)

register_exception_handlers(app)

app.include_router(router, prefix="/api")
