"""从年度报告 PDF 逐页取出词和坐标（pdfplumber）。

一页一页地给，用的人读够了就停；每页取完即释放，不把整份年报留在内存里。
"""

from __future__ import annotations

import io
import logging
from collections.abc import Iterator

from app.domain.securities.dataset import DatasetBuildError
from app.domain.securities.report_statements import PageWords, Word

DEFAULT_FIRST_PAGE = 12  # 前面是目录、释义、公司简介，没有财务报表
DEFAULT_MAX_BYTES = 128 * 1024 * 1024
# 最多读这么多页。存档的 117 份年报里，合并报表最晚在第 170 页之前结束；
# 报表是图片的年报一个标题都找不到，不封顶就会把整份几百页读完
DEFAULT_MAX_PAGES = 260

logging.getLogger("pdfminer").setLevel(logging.ERROR)


class PdfPlumberWordsReader:
    def __init__(
        self, *, max_bytes: int = DEFAULT_MAX_BYTES, max_pages: int = DEFAULT_MAX_PAGES
    ) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        if max_pages < 1:
            raise ValueError("max_pages must be positive")
        self._max_bytes = max_bytes
        self._max_pages = max_pages

    def read_words(
        self, pdf: bytes, *, first_page: int = DEFAULT_FIRST_PAGE
    ) -> Iterator[PageWords]:
        import pdfplumber  # noqa: PLC0415 - 只有抽取年报时才需要

        if first_page < 1:
            raise ValueError("first_page must be at least 1")
        if not pdf.startswith(b"%PDF-") or len(pdf) > self._max_bytes:
            raise DatasetBuildError("report_unreadable")
        try:
            with pdfplumber.open(io.BytesIO(pdf)) as document:
                last = min(len(document.pages), first_page - 1 + self._max_pages)
                for index in range(first_page - 1, last):
                    page = document.pages[index]
                    words = tuple(
                        Word(w["text"], float(w["x0"]), float(w["x1"]), float(w["top"]))
                        for w in page.extract_words(
                            x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False
                        )
                    )
                    page.flush_cache()
                    yield PageWords(index + 1, words)
        except DatasetBuildError:
            raise
        except Exception:
            raise DatasetBuildError("report_unreadable") from None
