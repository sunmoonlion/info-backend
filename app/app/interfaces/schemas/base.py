from typing import TypeVar, Generic, Optional, Any

from pydantic import BaseModel, Field

T = TypeVar("T")


class Response(BaseModel, Generic[T]):
    code: int = 200
    msg: str = "success"
    data: Any = Field(default=None)

    @staticmethod
    def success(data: Optional[T] = None, msg: str = "success") -> "Response[T]":
        return Response(code=200, msg=msg, data=data if data is not None else {})

    @staticmethod
    def fail(code: int, msg: str, data: Optional[T] = None) -> "Response[T]":
        return Response(code=code, msg=msg, data=data if data is not None else {})
