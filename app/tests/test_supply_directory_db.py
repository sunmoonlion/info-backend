"""证券名录（decisions ⑭「查主体」）：巨潮列表进库、按代码 / 简称 / 拼音查、一天至多排一次
刷新、经内部接口给 knowbench 查。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from test_durable_delivery_db import db as db
from test_durable_delivery_db import sql
from test_supply_cninfo import FakeHttp

from app.application.supply.cninfo import CninfoCatalog
from app.application.supply.directory import SecurityDirectory, SecurityEntry
from app.infrastructure.repositories.supply_directory import (
    REFRESH_TOPIC,
    SqlDirectoryRepository,
)


class Clock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 28, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.now


async def refreshes(db) -> int:
    return await sql(
        db, "select count(*) from outbox_message where topic = :t", t=REFRESH_TOPIC
    )


async def test_cninfo_lists_a_shares_with_suffix_name_and_pinyin():
    entries = await CninfoCatalog(FakeHttp()).securities()
    assert entries == [
        SecurityEntry("000001.SZ", "平安银行", "payh"),
        SecurityEntry("600009.SH", "上海机场", "shjc"),
        SecurityEntry("600519.SH", "贵州茅台", "gzmt"),
    ]


async def test_search_by_code_name_and_pinyin_and_refresh_once_a_day(db):
    clock = Clock()
    directory = SecurityDirectory(SqlDirectoryRepository(lambda: db), clock=clock)
    # 名录空着：排一次刷新，这次回空；同一天再查不重复排
    assert await directory.search("茅台") == []
    assert await directory.search("茅台") == []
    assert await refreshes(db) == 1

    assert await directory.refresh(CninfoCatalog(FakeHttp())) == 3
    assert [e.subject for e in await directory.search("茅台")] == ["600519.SH"]
    assert [e.subject for e in await directory.search("600519")] == ["600519.SH"]
    assert [e.subject for e in await directory.search("600009.sh")] == ["600009.SH"]
    assert [e.name for e in await directory.search("GZMT")] == ["贵州茅台"]
    # 通配符照字面认
    assert await directory.search("%") == []
    assert await refreshes(db) == 1

    # 过了一天：再排一次，库里的照回
    clock.now += timedelta(days=1, minutes=1)
    assert [e.subject for e in await directory.search("平安")] == ["000001.SZ"]
    assert await refreshes(db) == 2


async def test_knowbench_finds_securities_over_http(db):
    import httpx
    from fastapi import FastAPI

    from app.interfaces.errors.exception_handlers import register_exception_handlers
    from app.interfaces.http.internal import supply as routes

    directory = SecurityDirectory(SqlDirectoryRepository(lambda: db), clock=Clock())
    await directory.refresh(CninfoCatalog(FakeHttp()))
    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(routes.router, prefix="/api")
    app.dependency_overrides[routes.internal_host_only] = lambda: None
    app.dependency_overrides[routes.caller_app] = lambda: "knowbench"
    app.dependency_overrides[routes.get_directory] = lambda: directory
    client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://knowfeed"
    )
    found = await client.get("/api/internal/v1/supply/securities", params={"q": "机场"})
    assert found.status_code == 200, found.text
    assert found.json() == [{"subject": "600009.SH", "name": "上海机场"}]
    empty = await client.get("/api/internal/v1/supply/securities", params={"q": ""})
    assert empty.status_code == 422
