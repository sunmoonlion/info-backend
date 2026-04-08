import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.infrastructure.logging.logging import setup_logging
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

    try:
        yield
    finally:
        logger.info("应用正在关闭...")
        await get_redis().shutdown()
        await get_postgres().shutdown()
        logger.info("应用已关闭")


app = FastAPI(
    title="tpl Admin Backend",
    description="通用后台管理 API 服务",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

register_exception_handlers(app)

app.include_router(router, prefix="/api")
