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
        key=KEY,
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
        "source_url": "http://static.cninfo.com.cn/x.PDF",
        "period_start": "2025-01-01",
        "period_end": "2025-12-31",
        "disclosed_on": "2026-04-25",
    }
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
