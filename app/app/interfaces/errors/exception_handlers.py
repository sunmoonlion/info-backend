import logging

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException

from app.application.errors.exceptions import AppException
from app.interfaces.schemas.base import Response

logger = logging.getLogger(__name__)


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppException)
    async def app_exception_handler(req: Request, e: AppException) -> JSONResponse:
        logger.error(f"AppException: {e.msg}")
        return JSONResponse(status_code=e.status_code, content=Response.fail(e.code, e.msg).model_dump())

    @app.exception_handler(HTTPException)
    async def http_exception_handler(req: Request, e: HTTPException) -> JSONResponse:
        logger.error(f"HTTPException: {e.detail}")
        return JSONResponse(status_code=e.status_code, content=Response.fail(e.status_code, e.detail).model_dump())

    @app.exception_handler(Exception)
    async def exception_handler(req: Request, e: Exception) -> JSONResponse:
        logger.error(f"Exception: {e}")
        return JSONResponse(status_code=500, content=Response.fail(500, "服务器内部错误").model_dump())
