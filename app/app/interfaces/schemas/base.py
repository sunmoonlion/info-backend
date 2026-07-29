from typing import Any, Generic, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T")


class Response(BaseModel, Generic[T]):
    code: int = 200
    msg: str = "success"
    data: Any = Field(default=None)

    @staticmethod
    def success(data: T | None = None, msg: str = "success") -> "Response[T]":
        return Response(code=200, msg=msg, data=data if data is not None else {})

    @staticmethod
    def fail(code: int, msg: str, data: T | None = None) -> "Response[T]":
        return Response(code=code, msg=msg, data=data if data is not None else {})
