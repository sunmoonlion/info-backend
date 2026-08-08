from pathlib import Path

from app.infrastructure.storage.object_storage import (
    ObjectStorage,
    make_artifact_key,
)
from core.config import get_settings


def test_make_artifact_key_sanitizes_source_and_name() -> None:
    key = make_artifact_key(
        source_code="a/b c",
        date_path="2026-07-06",
        job_id="job-1",
        artifact_name="../raw.html",
    )

    assert key == "info/original/source=a-b-c/date=2026-07-06/job=job-1/..-raw.html"


def test_local_object_storage_put_bytes(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("STORAGE_LOCAL_ROOT", str(tmp_path))
    monkeypatch.setenv("S3_BUCKET", "development-info-originals")
    get_settings.cache_clear()

    try:
        storage = ObjectStorage()
        stored = storage.put_bytes(
            object_key="info/original/test/raw.html",
            data=b"<html>ok</html>",
            content_type="text/html",
        )

        assert stored.bucket == "development-info-originals"
        assert stored.object_key == "info/original/test/raw.html"
        assert stored.size_bytes == 15
        assert stored.sha256
        assert (
            tmp_path
            / "development-info-originals"
            / "info"
            / "original"
            / "test"
            / "raw.html"
        ).read_bytes() == b"<html>ok</html>"
    finally:
        get_settings.cache_clear()
