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


def _local(tmp_path: Path, monkeypatch) -> ObjectStorage:
    monkeypatch.setenv("STORAGE_BACKEND", "local")
    monkeypatch.setenv("STORAGE_LOCAL_ROOT", str(tmp_path))
    monkeypatch.setenv("S3_BUCKET", "development-info-originals")
    get_settings.cache_clear()
    return ObjectStorage()


def test_local_get_bytes_returns_what_was_stored(tmp_path: Path, monkeypatch) -> None:
    try:
        storage = _local(tmp_path, monkeypatch)
        stored = storage.put_bytes(
            object_key="info/securities/x/report.pdf",
            data=b"%PDF-1.4 body",
            content_type="application/pdf",
        )
        assert (
            storage.get_bytes(
                object_key=stored.object_key, expected_sha256=stored.sha256
            )
            == b"%PDF-1.4 body"
        )
        assert storage.get_bytes(object_key=stored.object_key) == b"%PDF-1.4 body"
    finally:
        get_settings.cache_clear()


def test_local_get_bytes_refuses_content_that_changed(
    tmp_path: Path, monkeypatch
) -> None:
    import pytest

    try:
        storage = _local(tmp_path, monkeypatch)
        stored = storage.put_bytes(
            object_key="info/securities/x/data.json",
            data=b'{"a": 1}',
            content_type="application/json",
        )
        path = tmp_path / "development-info-originals" / stored.object_key
        path.write_bytes(b'{"a": 2}')
        with pytest.raises(RuntimeError, match="stored_object_digest_mismatch"):
            storage.get_bytes(
                object_key=stored.object_key, expected_sha256=stored.sha256
            )
        with pytest.raises(RuntimeError, match="stored_object_too_large"):
            storage.get_bytes(object_key=stored.object_key, max_bytes=3)
    finally:
        get_settings.cache_clear()


def test_local_get_bytes_cannot_leave_the_storage_root(
    tmp_path: Path, monkeypatch
) -> None:
    import pytest

    try:
        storage = _local(tmp_path / "root", monkeypatch)
        (tmp_path / "root").mkdir()
        (tmp_path / "secret.txt").write_bytes(b"outside")
        for key in ("../../secret.txt", "../secret.txt", "/etc/hostname"):
            with pytest.raises((RuntimeError, OSError)):
                storage.get_bytes(object_key=key)
    finally:
        get_settings.cache_clear()
