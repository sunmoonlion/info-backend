"""knowfeed 调 knowbase 供给接口的请求形状：路径、参数、请求体照 knowbase 的接口与模型。

knowbase 那边的路由：`user_library_routes.py`（公开原件、引用）、`user_run_routes.py`
（加工授权、读法）、`user_fact_routes.py`（事实）、`user_resolution_routes.py`（核确认）。
"""

from __future__ import annotations

import json
from datetime import date

import httpx
import pytest

from app.domain.supply.models import DescriptionKey, SupplyError
from app.infrastructure.external.knowbase_supply import HttpKnowbase

KEY = DescriptionKey("000858.SZ", "annual_report", date(2025, 1, 1), date(2025, 12, 31))


class Tokens:
    async def get_token(self):
        return "service-token"


def client(handler) -> tuple[HttpKnowbase, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    return (
        HttpKnowbase(
            "http://knowbase/api/internal/v2/user-data",
            Tokens(),  # type: ignore[arg-type]
            5,
            transport=httpx.MockTransport(record),
        ),
        seen,
    )


async def test_requests_follow_the_knowbase_endpoints():
    replies = {
        "/service/public-originals": {
            "material_id": "m-1",
            "version_no": 1,
            "sha256": "a" * 64,
            "created": True,
        },
        "/service/public-originals/m-1/references": {"recorded": 2},
        "/processing/grants": {"grant_id": "g-1"},
    }

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/internal/v2/user-data")
        return httpx.Response(201, json=replies.get(path, {}))

    kb, seen = client(handler)
    made = await kb.deliver_public(
        channel="cninfo",
        channel_doc_id="000858:annual_report:2025",
        name="五粮液2025年年度报告.pdf",
        content=b"%PDF-1.7",
        subject=KEY.subject,
        doc_type=KEY.doc_type,
        period_start=KEY.period_start,
        period_end=KEY.period_end,
        edition="full",
        topics=(),
        disclosed_on="2026-04-25",
        source_url="http://static.cninfo.com.cn/x.PDF",
    )
    assert made["created"] is True
    deliver = seen[0]
    assert deliver.method == "POST" and deliver.content == b"%PDF-1.7"
    assert dict(deliver.url.params) == {
        "channel": "cninfo",
        "channel_doc_id": "000858:annual_report:2025",
        "name": "五粮液2025年年度报告.pdf",
        "subject": "000858.SZ",
        "doc_type": "annual_report",
        "edition": "full",
        "source_url": "http://static.cninfo.com.cn/x.PDF",
        "period_start": "2025-01-01",
        "period_end": "2025-12-31",
        "disclosed_on": "2026-04-25",
    }
    # 公告：不带报告期，事项一项一个参数（knowbase 收 topics 列表）
    await kb.deliver_public(
        channel="cninfo",
        channel_doc_id="000858:announcement:1",
        name="回购.pdf",
        content=b"%PDF-1.7",
        subject="000858.SZ",
        doc_type="interim_announcement",
        period_start=None,
        period_end=None,
        edition="full",
        topics=("equity", "supervision"),
        disclosed_on="2026-05-01",
        source_url="http://static.cninfo.com.cn/y.PDF",
    )
    announced = seen.pop(1)
    assert announced.url.params.get_list("topics") == ["equity", "supervision"]
    assert "period_end" not in announced.url.params
    assert deliver.headers["Authorization"] == "Bearer service-token"

    assert await kb.add_references("m-1", 1, [("alice", "demand:d:c1")]) == 2
    assert json.loads(seen[1].content) == {
        "version_no": 1,
        "references": [{"subject": "alice", "ref": "demand:d:c1"}],
    }
    assert await kb.processing_grant("m-1", 1) == "g-1"
    assert json.loads(seen[2].content) == {
        "owner": "public:disclosure",
        "material_id": "m-1",
        "version_no": 1,
    }
    await kb.write_pages("g-1", "p-1", [{"page": 1, "text": "x"}])
    assert seen[3].url.path.endswith("/processing/grants/g-1/renditions/pages")
    assert dict(seen[3].url.params) == {"processor_version": "p-1"}
    assert json.loads(seen[3].content) == [{"page": 1, "text": "x"}]
    await kb.write_failed("g-1", "table", "t-1", "scanned")
    assert dict(seen[4].url.params) == {
        "processor_version": "t-1",
        "failed_reason": "scanned",
    }
    await kb.write_facts("g-1", "t-1", [{"label": "营业收入"}], [])
    assert seen[5].url.path.endswith("/processing/grants/g-1/facts")
    assert json.loads(seen[5].content) == {
        "processor_version": "t-1",
        "raw": [{"label": "营业收入"}],
        "standard": [],
    }
    await kb.check_confirmation("conf-1", "alice")
    assert seen[6].method == "GET"
    assert seen[6].url.path.endswith("/service/confirmations/conf-1")
    assert dict(seen[6].url.params) == {"subject": "alice"}


async def test_refusals_keep_their_code_and_outages_become_retryable():
    kb, _ = client(lambda r: httpx.Response(403, json={"code": "service_scope_denied"}))
    with pytest.raises(SupplyError) as caught:
        await kb.processing_grant("m-1", 1)
    assert (caught.value.status, caught.value.code) == (403, "service_scope_denied")
    down, _ = client(lambda r: httpx.Response(502, text="bad gateway"))
    with pytest.raises(SupplyError) as caught:
        await down.processing_grant("m-1", 1)
    assert caught.value.status == 503  # 作业按可重试处理


async def test_processing_uploads_pending_list_grant_text_and_reading_by_grant():
    """上传资料的加工（0045 第 3.4 节）：待加工清单、本人的加工授权、正文写回（knowbase 契约
    「读法写回格式」：UTF-8 文字，可带 title、published_on）、凭授权读原件与交还。"""

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/internal/v2/user-data")
        if path == "/processing/pending":
            return httpx.Response(
                200, json=[{"owner": "alice", "material_id": "m-9", "version_no": 2}]
            )
        if path == "/processing/grants":
            return httpx.Response(201, json={"grant_id": "g-9"})
        return httpx.Response(201, json={})

    kb, seen = client(handler)
    assert await kb.pending_processing(50) == [("alice", "m-9", 2)]
    assert dict(seen[0].url.params) == {"limit": "50"}
    assert await kb.processing_grant("m-9", 2, owner="alice") == "g-9"
    assert json.loads(seen[1].content)["owner"] == "alice"
    await kb.write_text(
        "g-9", "text-1", "正文", title="标题", published_on=date(2026, 10, 1)
    )
    assert seen[2].url.path.endswith("/processing/grants/g-9/renditions/text")
    assert dict(seen[2].url.params) == {
        "processor_version": "text-1",
        "title": "标题",
        "published_on": "2026-10-01",
    }
    assert seen[2].content == "正文".encode()
    await kb.write_text("g-9", "text-1", "正文", title=None, published_on=None)
    assert dict(seen[3].url.params) == {"processor_version": "text-1"}


async def test_the_reader_reads_content_by_grant_and_releases_it():
    from app.infrastructure.external.knowbase_supply import HttpKnowbaseReader

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if "/run-grants/g-0/" in request.url.path:
            return httpx.Response(404, json={"code": "grant_not_found"})
        if request.url.path.endswith("/content"):
            return httpx.Response(
                200, content=b"%PDF-1.7", headers={"content-type": "application/pdf"}
            )
        if request.url.path.endswith("/release"):
            return httpx.Response(204)
        return httpx.Response(404, json={"code": "grant_not_found"})

    reader = HttpKnowbaseReader(
        "http://knowbase/api/internal/v2/user-data",
        Tokens(),  # type: ignore[arg-type]
        5,
        transport=httpx.MockTransport(handler),
    )
    assert await reader.content("g-9", "m-9", 2) == (b"%PDF-1.7", "application/pdf")
    assert seen[0].url.path == (
        "/api/internal/v2/user-data/run-grants/g-9/materials/m-9/content"
    )
    assert dict(seen[0].url.params) == {"version_no": "2"}
    await reader.release("g-9")
    assert (seen[1].method, seen[1].url.path) == (
        "POST",
        "/api/internal/v2/user-data/run-grants/g-9/release",
    )
    with pytest.raises(SupplyError) as caught:
        await reader.content("g-0", "m-0", 1)
    assert (caught.value.status, caught.value.code) == (404, "grant_not_found")
