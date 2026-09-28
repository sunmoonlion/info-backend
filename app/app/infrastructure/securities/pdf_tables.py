"""从年度报告 PDF 抽出前若干页的文字与表格（pdfplumber）。

只对文字里出现「主要会计数据」的页做表格抽取：表格抽取比文字抽取贵得多。
"""

from __future__ import annotations

import io
import logging

from app.domain.securities.dataset import DatasetBuildError, ReportPage

_MARKER = "主要会计数据"
DEFAULT_MAX_PDF_BYTES = 64 * 1024 * 1024

logging.getLogger("pdfminer").setLevel(logging.ERROR)


class PdfPlumberReportReader:
    def __init__(self, *, max_bytes: int = DEFAULT_MAX_PDF_BYTES) -> None:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        self._max_bytes = max_bytes

    def extract_pages(self, pdf: bytes, *, max_pages: int) -> list[ReportPage]:
        import pdfplumber  # noqa: PLC0415 - 只有建数据集时才需要

        if not pdf.startswith(b"%PDF-") or len(pdf) > self._max_bytes:
            raise DatasetBuildError("report_unreadable")
        pages: list[ReportPage] = []
        try:
            with pdfplumber.open(io.BytesIO(pdf)) as document:
                for index, page in enumerate(document.pages[:max_pages]):
                    text = page.extract_text() or ""
                    tables: tuple = ()
                    if _MARKER in text.replace(" ", ""):
                        tables = tuple(
                            tuple(tuple((cell or "") for cell in row) for row in table)
                            for table in page.extract_tables()
                            if table
                        )
                    pages.append(ReportPage(index + 1, tables, text))
        except DatasetBuildError:
            raise
        except Exception:
            raise DatasetBuildError("report_unreadable") from None
        return pages
