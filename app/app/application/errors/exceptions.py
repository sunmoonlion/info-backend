from typing import Any


class AppException(RuntimeError):
    def __init__(
        self,
        code: str = "request_invalid",
        status_code: int = 400,
        msg: str = "Request rejected",
        data: Any = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        self.msg = msg
        self.data = data
        super().__init__(msg)


class BadRequestError(AppException):
    def __init__(
        self, msg: str = "The request is invalid", code: str = "request_invalid"
    ) -> None:
        super().__init__(status_code=400, code=code, msg=msg)


class UnauthorizedError(AppException):
    def __init__(
        self, msg: str = "Authentication is required", code: str = "auth_required"
    ) -> None:
        super().__init__(status_code=401, code=code, msg=msg)


class ForbiddenError(AppException):
    def __init__(
        self, msg: str = "The request is not authorized", code: str = "forbidden"
    ) -> None:
        super().__init__(status_code=403, code=code, msg=msg)


class NotFoundError(AppException):
    def __init__(self, msg: str = "Resource not found", code: str = "not_found"):
        super().__init__(status_code=404, code=code, msg=msg)


class ValidationError(AppException):
    def __init__(
        self,
        msg: str = "The request payload is invalid",
        code: str = "invalid_request",
    ) -> None:
        super().__init__(status_code=422, code=code, msg=msg)


class ServerError(AppException):
    def __init__(self, msg: str = "The service could not complete the request"):
        super().__init__(status_code=500, code="internal_error", msg=msg)


class ServiceUnavailableError(AppException):
    def __init__(
        self,
        msg: str = "The service is temporarily unavailable",
        code: str = "provider_unavailable",
    ) -> None:
        super().__init__(status_code=503, code=code, msg=msg)


class BadGatewayError(AppException):
    def __init__(
        self,
        msg: str = "The downstream service returned an invalid response",
        code: str = "contract_invalid",
    ) -> None:
        super().__init__(status_code=502, code=code, msg=msg)


class ConcurrencyConflictError(AppException):
    def __init__(
        self,
        msg: str = "The requested cursor is no longer available",
        code: str = "cursor_expired",
    ) -> None:
        super().__init__(status_code=409, code=code, msg=msg)
