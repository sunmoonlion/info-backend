import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.application.errors.exceptions import AppException
from app.interfaces.schemas.base import Response

logger = logging.getLogger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppException)
    async def app_exception_handler(
        request: Request, error: AppException
    ) -> JSONResponse:
        logger.warning(
            "application_error status=%s code=%s path=%s",
            error.status_code,
            error.code,
            request.url.path,
        )
        return JSONResponse(
            status_code=error.status_code,
            content=Response.fail(error.code, error.msg).model_dump(),
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(
        request: Request, error: HTTPException
    ) -> JSONResponse:
        logger.warning(
            "http_error status=%s path=%s",
            error.status_code,
            request.url.path,
        )
        return JSONResponse(
            status_code=error.status_code,
            content=Response.fail(error.status_code, str(error.detail)).model_dump(),
        )

    @app.exception_handler(Exception)
    async def exception_handler(request: Request, error: Exception) -> JSONResponse:
        logger.exception(
            "unhandled_exception path=%s type=%s",
            request.url.path,
            type(error).__name__,
        )
        return JSONResponse(
            status_code=500,
            content=Response.fail(500, "服务器内部错误").model_dump(),
        )
