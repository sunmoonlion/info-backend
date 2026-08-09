from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from importlib.metadata import PackageNotFoundError, version

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import text
from starlette.middleware.trustedhost import TrustedHostMiddleware

from app.application.audit_context import (
    from_request,
    get_context,
    reset_context,
    set_context,
)
from app.infrastructure.logging.logging import setup_logging
from app.infrastructure.messaging.celery_producer import get_celery_producer
from app.infrastructure.storage.postgres import get_postgres
from app.infrastructure.storage.redis import get_redis
from app.interfaces.errors.exception_handlers import register_exception_handlers
from app.interfaces.http.routes import router
from core.config import Settings, get_settings

logger = logging.getLogger(__name__)
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

try:
    _PACKAGE_VERSION = version("info-backend")
except PackageNotFoundError:
    _PACKAGE_VERSION = "0+unknown"


def create_app(settings: Settings | None = None) -> FastAPI:
    runtime_settings = settings or get_settings()
    setup_logging()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        if runtime_settings.is_production or runtime_settings.casdoor_endpoint:
            runtime_settings.require_browser_identity()
        logger.info(
            "service_starting service=%s role=api", runtime_settings.service_name
        )
        await get_redis().init()
        await get_postgres().init()
        producer = get_celery_producer()
        logger.info(
            "celery_producer enabled=%s queue=%s",
            producer.enabled,
            runtime_settings.celery_queue,
        )
        try:
            yield
        finally:
            logger.info(
                "service_stopping service=%s role=api", runtime_settings.service_name
            )
            await get_redis().shutdown()
            await get_postgres().shutdown()

    application = FastAPI(
        title="Info Backend",
        description="One FastAPI Backend for separate Admin and Web browser surfaces",
        version=_PACKAGE_VERSION,
        lifespan=lifespan,
        docs_url=None if runtime_settings.is_production else "/docs",
        redoc_url=None if runtime_settings.is_production else "/redoc",
        openapi_url=None if runtime_settings.is_production else "/openapi.json",
    )

    @application.middleware("http")
    async def request_context_middleware(request: Request, call_next):
        context = from_request(request)
        token = set_context(context)
        request.state.audit_context = context
        status_code = 500
        try:
            response = await call_next(request)
            status_code = response.status_code
        finally:
            active_context = get_context() or context
            if request.method not in _SAFE_METHODS and request.url.path.startswith(
                "/api/"
            ):
                logger.info(
                    "audit_mutation method=%s path=%s status=%s actor_id=%s "
                    "correlation_id=%s operation_id=%s reason_present=%s",
                    request.method,
                    request.url.path,
                    status_code,
                    active_context.actor_id or "-",
                    active_context.correlation_id,
                    active_context.operation_id or "-",
                    bool(active_context.reason),
                )
            reset_context(token)

        response.headers["X-Correlation-ID"] = context.correlation_id
        response.headers["X-Operation-ID"] = (
            context.operation_id or context.correlation_id
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=()"
        )
        if request.url.path.startswith("/api/auth/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    application.add_middleware(
        TrustedHostMiddleware, allowed_hosts=list(runtime_settings.allowed_host_list)
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(runtime_settings.frontend_origin_list),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Accept",
            "Content-Type",
            "X-CSRF-Token",
            "X-Correlation-ID",
            "X-Operation-ID",
            "X-Audit-Reason",
        ],
        expose_headers=["X-Correlation-ID", "X-Operation-ID"],
    )
    register_exception_handlers(application)

    async def live() -> dict[str, str]:
        return {"status": "ok"}

    async def ready() -> JSONResponse:
        try:
            await get_redis().client.ping()  # type: ignore[misc]
            async with get_postgres().session_factory() as session:
                await session.execute(text("SELECT 1"))
        except Exception as exc:
            logger.warning("readiness_failed type=%s", type(exc).__name__)
            return JSONResponse(status_code=503, content={"status": "not_ready"})
        return JSONResponse(status_code=200, content={"status": "ready"})

    application.add_api_route(
        "/health/live", live, methods=["GET"], include_in_schema=False
    )
    application.add_api_route("/health", live, methods=["GET"], include_in_schema=False)
    application.add_api_route(
        "/api/health", ready, methods=["GET"], include_in_schema=False
    )
    application.add_api_route(
        "/health/ready", ready, methods=["GET"], include_in_schema=False
    )
    application.add_api_route("/ready", ready, methods=["GET"], include_in_schema=False)

    @application.get("/api/version", include_in_schema=False)
    async def api_version() -> dict[str, object]:
        return {
            "version": application.version,
            "deploymentId": runtime_settings.deployment_id,
            "contractVersion": 1,
        }

    application.include_router(router, prefix="/api")
    return application


settings = get_settings()
app = create_app(settings)
