"""取数边界的端口：取数失败的约定。具体实现在基础设施层（`securities/fetcher.py`）。"""

from __future__ import annotations


class FetchFailed(RuntimeError):
    """取数失败。code 是稳定错误码（来自取数边界），不含地址与参数。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
