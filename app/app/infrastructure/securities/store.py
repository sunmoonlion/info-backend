"""证券采集批次的留存与登记：对象存储 + PostgreSQL。

对象键由内容决定（含 sha256），所以同一内容重采落在同一个键上；登记表里已有相同键与
校验值的原文时不再写对象存储，只登记引用（MVP-02）。
"""

from __future__ import annotations

import hashlib
import re
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.domain.securities import (
    ArchivedItem,
    FetchRequest,
    IngestionStatus,
    RawResponse,
    SecurityCode,
    SourceCode,
)
from app.infrastructure.models.info import CrawlJob, InfoSource, RawArtifact
from app.infrastructure.models.securities import (
    SecurityIngestion,
    SecurityIngestionItem,
)
from app.infrastructure.storage.object_storage import ObjectStorage

_SAFE_NAME = re.compile(r"[^0-9A-Za-z._-]")

# 来源登记（F-INFO-03）。版权状态如实写：法定披露是公开信息；第三方网站的整理数据未确认
SOURCES: dict[str, dict[str, str]] = {
    SourceCode.CNINFO.value: {
        "name": "巨潮资讯网",
        "source_type": "disclosure_platform",
        "base_url": "http://www.cninfo.com.cn",
        "trust_level": "official",
        "copyright_status": "public_disclosure",
        "terms_url": "http://www.cninfo.com.cn",
        "description": "中国证监会指定的上市公司信息披露网站；采集公告列表与公告原文",
    },
    SourceCode.EASTMONEY_F10.value: {
        "name": "东方财富 F10 财务分析",
        "source_type": "website",
        "base_url": "https://emweb.securities.eastmoney.com",
        "trust_level": "third_party",
        "copyright_status": "unconfirmed_internal_only",
        "terms_url": "https://about.eastmoney.com",
        "description": "第三方网站整理的财务报表；再分发许可未确认，仅内部使用（D1、D2）",
    },
}


def security_object_key(*, code: str, source: str, sha256: str, name: str) -> str:
    safe = _SAFE_NAME.sub("-", name)[:120] or "object"
    return (
        f"info/securities/code={code}/source={source}/"
        f"sha256={sha256[:2]}/{sha256}/{safe}"
    )


class SqlIngestionStore:
    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        storage: ObjectStorage,
        clock: Callable[[], datetime],
    ) -> None:
        self._sessions = session_factory
        self._storage = storage
        self._clock = clock

    async def start(self, code: SecurityCode, *, sources: list[str]) -> str:
        async with self._sessions() as session:
            batch = await self.start_in(session, code, sources=sources)
            # 提交之后对象会过期（生产的会话是这样配的），要用的值先取出来
            ingestion_id = str(batch.id)
            await session.commit()
            return ingestion_id

    async def start_in(
        self, session: AsyncSession, code: SecurityCode, *, sources: list[str]
    ) -> SecurityIngestion:
        """在调用方的事务里登记批次；调用方负责提交（管理接口要和投递任务一起提交）。"""
        for source in sources:
            spec = SOURCES.get(source)
            if spec is None:
                raise ValueError(f"unregistered security source: {source}")
            await session.execute(
                pg_insert(InfoSource)
                .values(code=source, status="active", crawl_policy={}, **spec)
                .on_conflict_do_nothing(index_elements=[InfoSource.code])
            )
        batch = SecurityIngestion(
            security_code=code.code,
            market=str(code.market),
            status=IngestionStatus.PENDING.value,
            sources=list(sources),
            requested_at=self._clock(),
            summary={},
        )
        session.add(batch)
        await session.flush()
        return batch

    async def record_build_refusal(self, ingestion_id: str, code: str) -> None:
        """建库被拒绝：原文不足以建库，重投也不会变好。记在批次上。"""
        try:
            key = uuid.UUID(ingestion_id)
        except ValueError:
            return
        async with self._sessions() as session:
            batch = await session.get(SecurityIngestion, key)
            if batch is None:
                return
            batch.dataset_build_error = code[:80]
            batch.dataset_build_refused_at = self._clock()
            await session.commit()

    async def begin(self, ingestion_id: str) -> SecurityCode | None:
        try:
            key = uuid.UUID(ingestion_id)
        except ValueError:
            return None
        async with self._sessions() as session:
            batch = (
                await session.execute(
                    select(SecurityIngestion)
                    .where(SecurityIngestion.id == key)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if batch is None:
                return None
            if batch.status == IngestionStatus.RUNNING.value:
                batch.status = IngestionStatus.FAILED.value
                batch.finished_at = self._clock()
                batch.error_code = "interrupted"
                await session.commit()
                return None
            if batch.status != IngestionStatus.PENDING.value:
                return None
            code = SecurityCode(batch.security_code)
            batch.status = IngestionStatus.RUNNING.value
            batch.started_at = self._clock()
            await session.commit()
            return code

    async def archive(
        self,
        ingestion_id: str,
        *,
        code: SecurityCode,
        seq: int,
        request: FetchRequest,
        response: RawResponse,
    ) -> ArchivedItem:
        sha256 = hashlib.sha256(response.content).hexdigest()
        object_key = security_object_key(
            code=code.code,
            source=request.source.value,
            sha256=sha256,
            name=request.name,
        )
        now = self._clock()
        async with self._sessions() as session:
            source_id = (
                await session.execute(
                    select(InfoSource.id).where(InfoSource.code == request.source.value)
                )
            ).scalar_one()
            job = CrawlJob(
                source_id=source_id,
                job_type="security",
                target_url=request.url,
                final_url=response.final_url,
                status="succeeded" if response.status_code < 400 else "failed",
                http_status=response.status_code,
                attempt_count=1,
                started_at=now,
                finished_at=now,
                request={
                    "method": request.method,
                    "params": [list(p) for p in request.params],
                    "form": (
                        [list(p) for p in request.form]
                        if request.form is not None
                        else None
                    ),
                    "ingestion_id": ingestion_id,
                    "seq": seq,
                    "kind": request.kind.value,
                },
                response_metadata={
                    "content_type": response.content_type,
                    "decoded_from": response.decoded_from,
                },
                error_code=(
                    None
                    if response.status_code < 400
                    else f"http_status_{response.status_code}"
                ),
            )
            session.add(job)
            await session.flush()
            existing = (
                await session.execute(
                    select(RawArtifact)
                    .where(
                        RawArtifact.bucket == self._storage.bucket,
                        RawArtifact.object_key == object_key,
                        RawArtifact.sha256 == sha256,
                    )
                    .order_by(RawArtifact.created_at)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing is None:
                stored = self._storage.put_bytes(
                    object_key=object_key,
                    data=response.content,
                    content_type=response.content_type,
                    metadata={
                        "crawl_job_id": str(job.id),
                        "artifact_type": f"security_{request.kind.value}",
                        "security_code": code.code,
                    },
                )
                if stored.sha256 != sha256:
                    raise RuntimeError("stored object digest mismatch")
                artifact = RawArtifact(
                    crawl_job_id=job.id,
                    artifact_type=f"security_{request.kind.value}"[:50],
                    bucket=stored.bucket,
                    object_key=stored.object_key,
                    version_id=stored.version_id,
                    sha256=stored.sha256,
                    size_bytes=stored.size_bytes,
                    content_type=stored.content_type,
                    metadata_json={
                        "security_code": code.code,
                        "source": request.source.value,
                        "kind": request.kind.value,
                        **_jsonable(request.meta),
                    },
                )
                session.add(artifact)
                await session.flush()
            else:
                artifact = existing
            session.add(
                SecurityIngestionItem(
                    ingestion_id=uuid.UUID(ingestion_id),
                    seq=seq,
                    source_code=request.source.value,
                    kind=request.kind.value,
                    crawl_job_id=job.id,
                    raw_artifact_id=artifact.id,
                    sha256=sha256,
                    size_bytes=len(response.content),
                    reused=existing is not None,
                    http_status=response.status_code,
                    meta=_jsonable(request.meta),
                )
            )
            archived_key = artifact.object_key
            await session.commit()
            return ArchivedItem(
                seq=seq,
                source=request.source,
                kind=request.kind,
                sha256=sha256,
                size_bytes=len(response.content),
                object_key=archived_key,
                reused=existing is not None,
                http_status=response.status_code,
                meta=dict(request.meta),
            )

    async def finish(
        self,
        ingestion_id: str,
        *,
        status: IngestionStatus,
        summary: Mapping[str, Any],
        error_code: str | None = None,
        error_detail: str | None = None,
    ) -> None:
        async with self._sessions() as session:
            batch = await session.get(SecurityIngestion, uuid.UUID(ingestion_id))
            if batch is None:
                raise ValueError(f"security ingestion not found: {ingestion_id}")
            batch.status = status.value
            batch.finished_at = self._clock()
            batch.summary = _jsonable(dict(summary))
            batch.error_code = error_code
            batch.error_detail = error_detail
            await session.commit()


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_jsonable(v) for v in value]
    if isinstance(value, str | int | float | bool) or value is None:
        return value
    return str(value)
