from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from core.config import get_settings


@dataclass(frozen=True)
class StoredObject:
    bucket: str
    object_key: str
    version_id: str | None
    sha256: str
    size_bytes: int
    content_type: str


class ObjectStorage:
    def __init__(self) -> None:
        self._settings = get_settings()

    @property
    def bucket(self) -> str:
        return self._settings.s3_bucket

    def put_bytes(
        self,
        *,
        object_key: str,
        data: bytes,
        content_type: str,
        metadata: Mapping[str, str] | None = None,
    ) -> StoredObject:
        sha256 = hashlib.sha256(data).hexdigest()
        if self._settings.storage_backend.lower() == "s3":
            return self._put_s3(
                object_key=object_key,
                data=data,
                content_type=content_type,
                sha256=sha256,
                metadata=metadata,
            )
        return self._put_local(
            object_key=object_key,
            data=data,
            content_type=content_type,
            sha256=sha256,
            metadata=metadata,
        )

    def put_json(self, *, object_key: str, payload: object) -> StoredObject:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        return self.put_bytes(
            object_key=object_key,
            data=data,
            content_type="application/json; charset=utf-8",
        )

    def _put_local(
        self,
        *,
        object_key: str,
        data: bytes,
        content_type: str,
        sha256: str,
        metadata: Mapping[str, str] | None,
    ) -> StoredObject:
        root = Path(self._settings.storage_local_root)
        target = root / self.bucket / object_key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        if metadata:
            meta_path = target.with_suffix(target.suffix + ".metadata.json")
            meta_path.write_text(json.dumps(dict(metadata), ensure_ascii=False, indent=2))
        return StoredObject(
            bucket=self.bucket,
            object_key=object_key,
            version_id=None,
            sha256=sha256,
            size_bytes=len(data),
            content_type=content_type,
        )

    def _put_s3(
        self,
        *,
        object_key: str,
        data: bytes,
        content_type: str,
        sha256: str,
        metadata: Mapping[str, str] | None,
    ) -> StoredObject:
        import boto3
        from botocore.config import Config

        endpoint_url = self._settings.s3_endpoint
        addressing_style = "path" if self._settings.s3_force_path_style else "virtual"
        client = boto3.client(
            "s3",
            endpoint_url=endpoint_url,
            region_name=self._settings.s3_region,
            aws_access_key_id=self._settings.s3_access_key_id,
            aws_secret_access_key=self._settings.s3_secret_access_key,
            use_ssl=self._settings.s3_use_tls,
            config=Config(s3={"addressing_style": addressing_style}),
        )
        response = client.put_object(
            Bucket=self.bucket,
            Key=object_key,
            Body=data,
            ContentType=content_type,
            Metadata=dict(metadata or {}) | {"sha256": sha256},
        )
        head = client.head_object(Bucket=self.bucket, Key=object_key)
        size = int(head["ContentLength"])
        if size != len(data):
            raise RuntimeError(
                f"S3 object size mismatch for {object_key}: expected {len(data)}, got {size}"
            )
        return StoredObject(
            bucket=self.bucket,
            object_key=object_key,
            version_id=response.get("VersionId"),
            sha256=sha256,
            size_bytes=size,
            content_type=content_type,
        )


def make_artifact_key(
    *,
    source_code: str,
    date_path: str,
    job_id: str,
    artifact_name: str,
) -> str:
    safe_source = "".join(c if c.isalnum() or c in "-_" else "-" for c in source_code)
    safe_name = artifact_name.replace("/", "-").replace("\\", "-")
    return f"info/original/source={safe_source}/date={date_path}/job={job_id}/{safe_name}"


def get_object_storage() -> ObjectStorage:
    return ObjectStorage()
