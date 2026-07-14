from typing import Any


class AppException(RuntimeError):
    def __init__(self, code: int = 400, status_code: int = 400, msg: str = "应用发生错误", data: Any = None):
        self.code = code
        self.status_code = status_code
        self.msg = msg
        self.data = data
        super().__init__(msg)


class BadRequestError(AppException):
    def __init__(self, msg: str = "请求参数错误"):
        super().__init__(status_code=400, code=400, msg=msg)


class UnauthorizedError(AppException):
    def __init__(self, msg: str = "未登录或登录已过期"):
        super().__init__(status_code=401, code=401, msg=msg)


class ForbiddenError(AppException):
    def __init__(self, msg: str = "无权限访问"):
        super().__init__(status_code=403, code=403, msg=msg)


class NotFoundError(AppException):
    def __init__(self, msg: str = "资源不存在"):
        super().__init__(status_code=404, code=404, msg=msg)


class ValidationError(AppException):
    def __init__(self, msg: str = "数据校验失败"):
        super().__init__(status_code=422, code=422, msg=msg)


class ServerError(AppException):
    def __init__(self, msg: str = "服务器内部错误"):
        super().__init__(status_code=500, code=500, msg=msg)


class ServiceUnavailableError(AppException):
    def __init__(self, msg: str = "服务暂时不可用"):
        super().__init__(status_code=503, code=503, msg=msg)


class ConcurrencyConflictError(AppException):
    def __init__(self, msg: str = "资源已被其他操作更新，请刷新后重试"):
        super().__init__(status_code=409, code=409, msg=msg)
