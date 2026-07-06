from app.application.services.info_crawl_service import _decode_upload_text


def test_decode_upload_text_accepts_markdown() -> None:
    assert _decode_upload_text(b"# Title", "text/markdown", "note.md") == "# Title"


def test_decode_upload_text_defers_pdf_to_tools() -> None:
    assert _decode_upload_text(b"%PDF", "application/pdf", "report.pdf") is None
