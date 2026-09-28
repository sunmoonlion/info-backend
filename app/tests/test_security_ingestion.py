"""证券采集（0008-info 段一）：领域、两个采集器、采集服务。

夹具是 2026-09-27 录下的真实响应，只裁了行数。测试不访问外网，不需要数据库。
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from app.application.ports.securities import FetchFailed
from app.application.securities.collectors import (
    CninfoDisclosureCollector,
    EastmoneyF10StatementCollector,
)
from app.application.securities.ingestion_service import SecurityIngestionService
from app.domain.securities import (
    ArchivedItem,
    CollectError,
    FetchRequest,
    IngestionNotRunnable,
    IngestionStatus,
    InvalidSecurityCode,
    ItemKind,
    RawResponse,
    SecurityCode,
    SourceCode,
)

FIXTURES = Path(__file__).parent / "fixtures" / "securities"
PDF = b"%PDF-1.4\n% fixture, not a real report\n"
TODAY = date(2026, 9, 27)


def fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


class FakeFetcher:
    """按地址返回夹具。overrides 用来模拟某个地址出错或返回别的内容。"""

    def __init__(self, overrides: dict[str, object] | None = None) -> None:
        self.requests: list[FetchRequest] = []
        self.overrides = overrides or {}

    async def fetch(self, request: FetchRequest) -> RawResponse:
        self.requests.append(request)
        for marker, value in self.overrides.items():
            if marker in request.url:
                if isinstance(value, list):  # 依次用完，之后回到正常响应
                    if not value:
                        break
                    value = value.pop(0)
                if isinstance(value, Exception):
                    raise value
                assert isinstance(value, RawResponse)
                return value
        return RawResponse(200, *self._body(request), final_url=request.url)

    def _body(self, request: FetchRequest) -> tuple[str, bytes]:
        url = request.url
        if url.endswith("/new/data/szse_stock.json"):
            return "application/json", fixture("cninfo_stock_list.json")
        if url.endswith("/new/hisAnnouncement/query"):
            return "application/json", fixture("cninfo_annual_query.json")
        if url.startswith("http://static.cninfo.com.cn/finalpage/"):
            return "application/pdf", PDF + url.encode()
        if url.endswith("/Index"):
            return "text/html", fixture("em_f10_index.html")
        for prefix, name in (
            ("zcfzb", "balance"),
            ("lrb", "income"),
            ("xjllb", "cashflow"),
        ):
            if url.endswith(f"/{prefix}DateAjaxNew"):
                return "application/json", fixture(f"em_{name}_periods.json")
            if url.endswith(f"/{prefix}AjaxNew"):
                # 真实接口按请求的报告期返回；夹具里只有两行，把期数带进内容以区分各批
                body = json.loads(fixture(f"em_{name}_data.json"))
                body["requested"] = dict(request.params)["dates"]
                return "application/json", json.dumps(body).encode()
        raise AssertionError(f"unexpected request {url}")


class FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.batches: dict[str, dict] = {}
        self.items: list[tuple[str, ArchivedItem]] = []

    async def start(self, code: SecurityCode, *, sources: list[str]) -> str:
        ingestion_id = f"batch-{len(self.batches) + 1}"
        self.batches[ingestion_id] = {
            "code": code.code,
            "sources": sources,
            "status": IngestionStatus.PENDING,
        }
        return ingestion_id

    async def begin(self, ingestion_id: str) -> SecurityCode | None:
        batch = self.batches.get(ingestion_id)
        if batch is None or batch["status"] is not IngestionStatus.PENDING:
            return None
        batch["status"] = IngestionStatus.RUNNING
        return SecurityCode(batch["code"])

    async def archive(self, ingestion_id, *, code, seq, request, response):
        sha = hashlib.sha256(response.content).hexdigest()
        key = f"code={code}/source={request.source.value}/{sha}/{request.name}"
        reused = key in self.objects
        self.objects.setdefault(key, response.content)
        item = ArchivedItem(
            seq=seq,
            source=request.source,
            kind=request.kind,
            sha256=sha,
            size_bytes=len(response.content),
            object_key=key,
            reused=reused,
            http_status=response.status_code,
            meta=dict(request.meta),
        )
        self.items.append((ingestion_id, item))
        return item

    async def finish(
        self, ingestion_id, *, status, summary, error_code=None, error_detail=None
    ):
        self.batches[ingestion_id].update(
            status=status,
            summary=dict(summary),
            error_code=error_code,
            error_detail=error_detail,
        )


class Context:
    def __init__(self, fetcher: FakeFetcher) -> None:
        self.fetcher = fetcher
        self.skipped: list[tuple[str, str]] = []

    async def fetch(self, request: FetchRequest) -> bytes:
        try:
            response = await self.fetcher.fetch(request)
        except FetchFailed as exc:
            raise CollectError("fetch_failed", exc.code) from None
        if response.status_code >= 400:
            raise CollectError("http_status", str(response.status_code))
        return response.content

    async def fetch_optional(self, request: FetchRequest) -> bytes | None:
        try:
            return await self.fetch(request)
        except CollectError as exc:
            if exc.code not in ("fetch_failed", "http_status"):
                raise
            self.skipped.append((request.name, exc.detail))
            return None


def service(
    fetcher: FakeFetcher,
    store: FakeStore,
    pauses: list[int] | None = None,
    backoffs: list[int] | None = None,
):
    ticks = iter(range(1000))

    async def pause() -> None:
        if pauses is not None:
            pauses.append(1)

    async def backoff(attempt: int) -> None:
        if backoffs is not None:
            backoffs.append(attempt)

    return SecurityIngestionService(
        fetcher=fetcher,
        store=store,
        collectors=[
            CninfoDisclosureCollector(today=TODAY),
            EastmoneyF10StatementCollector(),
        ],
        clock=lambda: datetime(2026, 9, 27, 8, 0, next(ticks) % 60, tzinfo=UTC),
        pause=pause,
        backoff=backoff,
    )


# ---------------------------------------------------------------- 领域


@pytest.mark.parametrize(
    ("code", "market", "prefixed"),
    [
        ("600009", "SH", "SH600009"),
        ("688981", "SH", "SH688981"),
        ("000001", "SZ", "SZ000001"),
        ("300750", "SZ", "SZ300750"),
        ("430047", "BJ", "BJ430047"),
        ("835185", "BJ", "BJ835185"),
        ("920002", "BJ", "BJ920002"),
    ],
)
def test_security_code_knows_its_market(code, market, prefixed):
    parsed = SecurityCode(code)
    assert (parsed.market, parsed.prefixed, str(parsed)) == (market, prefixed, code)


@pytest.mark.parametrize(
    "code",
    [
        "",
        "60000",
        "6000090",
        "60000a",
        " 600009",
        "600009\n",
        "SH600009",
        "900901",
        "200002",
        "100001",
        "500001",
        "700001",
        None,
        600009,
    ],
)
def test_security_code_rejects_what_is_not_an_a_share(code):
    with pytest.raises(InvalidSecurityCode):
        SecurityCode(code)


# ---------------------------------------------------------------- 巨潮


async def test_cninfo_collects_full_annual_reports_only():
    fetcher = FakeFetcher()
    await CninfoDisclosureCollector(today=TODAY).collect(
        SecurityCode("600009"), Context(fetcher)
    )
    kinds = [r.kind for r in fetcher.requests]
    assert kinds[:2] == [ItemKind.STOCK_LIST, ItemKind.ANNOUNCEMENT_QUERY]
    reports = [r for r in fetcher.requests if r.kind is ItemKind.REPORT_FILE]
    assert len(reports) == 9 and len(kinds) == 11  # 2017 至 2025 年，摘要不要
    assert sorted(r.meta["fiscal_year"] for r in reports) == list(range(2017, 2026))
    assert all("摘要" not in r.meta["title"] for r in reports)
    assert all(
        r.url.startswith("http://static.cninfo.com.cn/finalpage/") for r in reports
    )
    assert all(r.source is SourceCode.CNINFO for r in fetcher.requests)


async def test_cninfo_query_is_a_post_with_the_org_id_from_the_stock_list():
    fetcher = FakeFetcher()
    await CninfoDisclosureCollector(today=TODAY, years=8).collect(
        SecurityCode("600009"), Context(fetcher)
    )
    query = fetcher.requests[1]
    form = dict(query.form or ())
    assert query.method == "POST" and query.params == ()
    assert form["stock"] == "600009,gssh0600009"
    assert form["category"] == "category_ndbg_szsh"
    assert form["seDate"] == "2018-01-01~2026-09-27"


async def test_cninfo_disclosure_date_is_the_official_one_in_shanghai_time():
    fetcher = FakeFetcher()
    await CninfoDisclosureCollector(today=TODAY).collect(
        SecurityCode("600009"), Context(fetcher)
    )
    by_year = {
        r.meta["fiscal_year"]: r.meta
        for r in fetcher.requests
        if r.kind is ItemKind.REPORT_FILE
    }
    # 第三方数据里这三份的公告日是 2019-02-21、2022-02-26、2023-03-25，都不对
    assert by_year[2018]["official_disclosed_date"] == "2019-03-23"
    assert by_year[2021]["official_disclosed_date"] == "2022-04-16"
    assert by_year[2022]["official_disclosed_date"] == "2023-04-29"
    assert by_year[2025]["official_disclosed_date"] == "2026-04-30"
    assert by_year[2025]["announcement_id"] == "1225264362"
    assert by_year[2025]["revised"] is False


@pytest.mark.parametrize(
    "adjunct",
    [
        "http://evil.example/finalpage/2026-04-30/1225264362.PDF",
        "//evil.example/finalpage/2026-04-30/1.PDF",
        "../finalpage/2026-04-30/1225264362.PDF",
        "finalpage/2026-04-30/../../etc/passwd",
        "finalpage/2026-04-30/1225264362.PDF?x=1",
        "finalpage/2026-04-30/1225264362.exe",
        "",
    ],
)
async def test_cninfo_never_follows_an_attachment_outside_the_archive(adjunct):
    payload = json.loads(fixture("cninfo_annual_query.json"))
    payload["announcements"] = payload["announcements"][:1]
    payload["announcements"][0]["adjunctUrl"] = adjunct
    payload["totalAnnouncement"] = 1
    fetcher = FakeFetcher(
        {
            "hisAnnouncement/query": RawResponse(
                200, "application/json", json.dumps(payload).encode(), "x"
            )
        }
    )
    with pytest.raises(CollectError, match="no_annual_report_found"):
        await CninfoDisclosureCollector(today=TODAY).collect(
            SecurityCode("600009"), Context(fetcher)
        )
    assert [r.kind for r in fetcher.requests] == [
        ItemKind.STOCK_LIST,
        ItemKind.ANNOUNCEMENT_QUERY,
    ]


async def test_cninfo_reports_a_code_the_source_does_not_know():
    with pytest.raises(CollectError, match="security_not_found_at_source"):
        await CninfoDisclosureCollector(today=TODAY).collect(
            SecurityCode("603999"), Context(FakeFetcher())
        )


async def test_cninfo_keeps_revised_reports_and_marks_them():
    payload = json.loads(fixture("cninfo_annual_query.json"))
    first = dict(payload["announcements"][0])
    first.update(
        announcementTitle="上海机场2025年年度报告（修订版）",
        announcementId="1225999999",
        adjunctUrl="finalpage/2026-05-20/1225999999.PDF",
    )
    payload["announcements"].append(first)
    payload["totalAnnouncement"] += 1
    fetcher = FakeFetcher(
        {
            "hisAnnouncement/query": RawResponse(
                200, "application/json", json.dumps(payload).encode(), "x"
            )
        }
    )
    await CninfoDisclosureCollector(today=TODAY).collect(
        SecurityCode("600009"), Context(fetcher)
    )
    of_2025 = [
        r.meta
        for r in fetcher.requests
        if r.kind is ItemKind.REPORT_FILE and r.meta["fiscal_year"] == 2025
    ]
    assert sorted(m["revised"] for m in of_2025) == [False, True]


# ---------------------------------------------------------------- 东方财富


async def test_eastmoney_fetches_periods_then_data_in_batches_of_five():
    fetcher = FakeFetcher()
    await EastmoneyF10StatementCollector().collect(
        SecurityCode("600009"), Context(fetcher)
    )
    assert fetcher.requests[0].kind is ItemKind.COMPANY_TYPE_PAGE
    assert dict(fetcher.requests[0].params) == {"type": "web", "code": "sh600009"}
    data = [r for r in fetcher.requests if r.kind is ItemKind.STATEMENT_DATA]
    periods = [r for r in fetcher.requests if r.kind is ItemKind.STATEMENT_PERIODS]
    assert [r.meta["statement"] for r in periods] == [
        "balance_sheet",
        "income_statement",
        "cash_flow",
    ]
    assert len(data) == 6  # 每张表 7 期：5 + 2
    first, second = data[0], data[1]
    assert dict(first.params)["dates"] == (
        "2026-06-30,2026-03-31,2025-12-31,2025-09-30,2025-06-30"
    )
    assert dict(second.params)["dates"] == "2025-03-31,2024-12-31"
    assert dict(first.params) | {"dates": ""} == {
        "companyType": "4",
        "reportDateType": "0",
        "reportType": "1",
        "dates": "",
        "code": "SH600009",
    }
    assert first.meta["periods"][0] == "2026-06-30"
    assert len({r.name for r in fetcher.requests}) == len(fetcher.requests)


@pytest.mark.parametrize("company_type", ["1", "2", "3"])
async def test_eastmoney_refuses_financial_companies(company_type):
    page = f'<input id="hidctype" type="hidden" value="{company_type}" />'
    fetcher = FakeFetcher({"/Index": RawResponse(200, "text/html", page.encode(), "x")})
    with pytest.raises(CollectError) as caught:
        await EastmoneyF10StatementCollector().collect(
            SecurityCode("601398"), Context(fetcher)
        )
    assert caught.value.code == "unsupported_company_type"
    assert len(fetcher.requests) == 1


@pytest.mark.parametrize(
    ("marker", "body"),
    [
        ("/Index", b"<html>no such input</html>"),
        ("zcfzbDateAjaxNew", b"<html>blocked</html>"),
        ("zcfzbDateAjaxNew", b'{"data": []}'),
        ("zcfzbDateAjaxNew", b'{"data": [{"REPORT_DATE": "soon"}]}'),
        ("zcfzbAjaxNew", b'{"message": "none"}'),
        ("zcfzbAjaxNew", b'{"data": []}'),
    ],
)
async def test_eastmoney_says_so_when_the_response_is_not_what_it_expects(marker, body):
    fetcher = FakeFetcher({marker: RawResponse(200, "text/html", body, "x")})
    with pytest.raises(CollectError) as caught:
        await EastmoneyF10StatementCollector().collect(
            SecurityCode("600009"), Context(fetcher)
        )
    assert caught.value.code == "unexpected_response"


async def test_eastmoney_covers_the_beijing_exchange_with_its_own_prefix():
    """三个市场的接口相同，只是代码前缀不同（北交所 2026-09-28 实测）。"""
    fetcher = FakeFetcher()
    await EastmoneyF10StatementCollector().collect(
        SecurityCode("920185"), Context(fetcher)
    )
    codes = {dict(r.params)["code"] for r in fetcher.requests}
    assert codes == {"bj920185", "BJ920185"}  # 类型页用小写，数据接口用大写
    assert len(fetcher.requests) == 10


# ---------------------------------------------------------------- 年报原文的大小与缺失


async def test_reports_ask_for_their_own_size_and_time_limits():
    fetcher = FakeFetcher()
    await CninfoDisclosureCollector(
        today=TODAY, max_report_bytes=64 * 1024 * 1024, report_timeout_seconds=90
    ).collect(SecurityCode("600009"), Context(fetcher))
    reports = [r for r in fetcher.requests if r.kind is ItemKind.REPORT_FILE]
    others = [r for r in fetcher.requests if r.kind is not ItemKind.REPORT_FILE]
    assert reports and all(
        (r.max_bytes, r.timeout_seconds) == (64 * 1024 * 1024, 90) for r in reports
    )
    assert all(r.max_bytes is None and r.timeout_seconds is None for r in others)


def test_report_limits_must_be_positive():
    for kwargs in ({"max_report_bytes": 0}, {"report_timeout_seconds": 0}):
        with pytest.raises(ValueError):
            CninfoDisclosureCollector(today=TODAY, **kwargs)


@pytest.mark.parametrize(
    ("failure", "detail"),
    [
        (FetchFailed("crawl_response_too_large"), "crawl_response_too_large"),
        (FetchFailed("crawl_deadline_exceeded"), "crawl_deadline_exceeded"),
        (RawResponse(404, "text/html", b"gone", "x"), "404"),
    ],
)
async def test_one_report_that_cannot_be_fetched_does_not_stop_the_others(
    failure, detail
):
    names = [
        r.name
        for r in await reports_requested(FakeFetcher())
        if r.kind is ItemKind.REPORT_FILE
    ]
    assert len(names) >= 3
    broken = names[1]
    marker = broken.split("-")[2].removesuffix(".pdf")
    context = Context(FakeFetcher({f"{marker}.PDF": [failure, failure, failure]}))
    await CninfoDisclosureCollector(today=TODAY).collect(
        SecurityCode("600009"), context
    )
    assert context.skipped == [(broken, detail)]
    fetched = [
        r.name for r in context.fetcher.requests if r.kind is ItemKind.REPORT_FILE
    ]
    assert set(fetched) == set(names)  # 后面的年报照常去取


async def reports_requested(fetcher: FakeFetcher) -> list[FetchRequest]:
    await CninfoDisclosureCollector(today=TODAY).collect(
        SecurityCode("600009"), Context(fetcher)
    )
    return fetcher.requests


async def test_when_no_report_at_all_can_be_fetched_the_source_fails():
    fetcher = FakeFetcher(
        {"static.cninfo.com.cn": FetchFailed("crawl_response_too_large")}
    )
    with pytest.raises(CollectError, match="no_annual_report_downloaded"):
        await CninfoDisclosureCollector(today=TODAY).collect(
            SecurityCode("600009"), Context(fetcher)
        )


async def test_a_skipped_report_is_in_the_summary_and_the_batch_succeeds():
    names = [
        r.name
        for r in await reports_requested(FakeFetcher())
        if r.kind is ItemKind.REPORT_FILE
    ]
    marker = names[0].split("-")[2].removesuffix(".pdf")
    fetcher = FakeFetcher({f"{marker}.PDF": FetchFailed("crawl_response_too_large")})
    store = FakeStore()
    summary = await service(fetcher, store).ingest("600009")
    assert summary.status is IngestionStatus.SUCCEEDED
    assert summary.items == 20  # 少了取不到的那一份，报表数据照常采了
    assert summary.statement_periods  # 报表数据在
    (skipped,) = summary.skipped
    assert (skipped.name, skipped.error_code, skipped.error_detail) == (
        names[0],
        "fetch_failed",
        "crawl_response_too_large",
    )
    assert skipped.kind is ItemKind.REPORT_FILE
    year = int(names[0].split("-")[1])
    assert year not in summary.report_years
    (saved,) = store.batches.values()
    assert saved["summary"]["skipped"] == [
        {
            "source": "cninfo",
            "kind": skipped.kind.value,
            "name": names[0],
            "error_code": "fetch_failed",
            "error_detail": "crawl_response_too_large",
            "fiscal_year": year,
        }
    ]


async def test_a_batch_level_problem_is_not_swallowed_as_a_skipped_report():
    """请求数超限是批次的问题，不是某一份年报的问题。"""
    import app.application.securities.ingestion_service as module

    original = module._MAX_REQUESTS
    module._MAX_REQUESTS = 4  # 股票列表、公告查询各一次，之后第三份年报就超限
    try:
        summary = await service(FakeFetcher(), FakeStore()).ingest("600009")
    finally:
        module._MAX_REQUESTS = original
    assert summary.status is IngestionStatus.FAILED
    assert summary.error_code == "too_many_requests" and summary.skipped == ()


# ---------------------------------------------------------------- 采集服务


async def test_ingest_archives_every_response_and_summarizes():
    fetcher, store, pauses = FakeFetcher(), FakeStore(), []
    summary = await service(fetcher, store, pauses).ingest("600009")
    assert summary.status is IngestionStatus.SUCCEEDED
    assert summary.security_code == "600009" and summary.error_code is None
    assert summary.items == len(fetcher.requests) == 21
    assert summary.new_artifacts == 21 and summary.reused_artifacts == 0
    assert summary.by_source == {"cninfo": 11, "eastmoney-f10": 10}
    assert summary.report_years == tuple(range(2017, 2026))
    assert summary.statement_periods == {
        "balance_sheet": 7,
        "cash_flow": 7,
        "income_statement": 7,
    }
    assert summary.bytes_fetched == sum(len(v) for v in store.objects.values())
    assert [i.seq for _, i in store.items] == list(range(1, 22))
    assert len(pauses) == 20  # 第一个请求之前不停
    batch = store.batches[summary.ingestion_id]
    assert batch["status"] is IngestionStatus.SUCCEEDED
    assert batch["sources"] == ["cninfo", "eastmoney-f10"]
    assert batch["summary"]["report_years"] == list(range(2017, 2026))


async def test_second_ingest_of_the_same_content_adds_no_artifact():
    store = FakeStore()
    first = await service(FakeFetcher(), store).ingest("600009")
    objects_after_first = dict(store.objects)
    second = await service(FakeFetcher(), store).ingest("600009")
    assert first.ingestion_id != second.ingestion_id
    assert second.status is IngestionStatus.SUCCEEDED
    assert (second.new_artifacts, second.reused_artifacts) == (0, 21)
    assert store.objects == objects_after_first


async def test_invalid_code_is_refused_before_a_batch_exists():
    fetcher, store = FakeFetcher(), FakeStore()
    with pytest.raises(InvalidSecurityCode):
        await service(fetcher, store).ingest("SH600009")
    assert store.batches == {} and fetcher.requests == []


async def test_http_error_fails_the_batch_but_keeps_the_response():
    fetcher = FakeFetcher(
        {"lrbDateAjaxNew": RawResponse(503, "text/html", b"busy", "x")}
    )
    store = FakeStore()
    summary = await service(fetcher, store).ingest("600009")
    assert summary.status is IngestionStatus.FAILED
    assert (summary.error_code, summary.error_detail) == ("http_status", "503")
    last = store.items[-1][1]
    assert last.http_status == 503 and store.objects[last.object_key] == b"busy"
    assert summary.statement_periods == {"balance_sheet": 7}
    assert store.batches[summary.ingestion_id]["error_code"] == "http_status"


async def test_unreadable_response_fails_the_batch_but_keeps_the_response():
    blocked = b"<html>please verify you are human</html>"
    fetcher = FakeFetcher(
        {"hisAnnouncement/query": RawResponse(200, "text/html", blocked, "x")}
    )
    store = FakeStore()
    summary = await service(fetcher, store).ingest("600009")
    assert summary.status is IngestionStatus.FAILED
    assert summary.error_code == "unexpected_response"
    assert summary.items == 2 and store.items[-1][1].kind is ItemKind.ANNOUNCEMENT_QUERY
    assert blocked in store.objects.values()
    assert summary.by_source == {"cninfo": 2}  # 第一个来源失败后不再请求第二个


async def test_transport_failure_fails_the_batch_with_a_stable_code():
    fetcher = FakeFetcher({"szse_stock.json": FetchFailed("crawl_deadline_exceeded")})
    store = FakeStore()
    summary = await service(fetcher, store).ingest("600009")
    assert summary.status is IngestionStatus.FAILED
    assert (summary.error_code, summary.error_detail) == (
        "fetch_failed",
        "crawl_deadline_exceeded",
    )
    assert summary.items == 0 and store.objects == {}


def test_a_service_without_collectors_is_a_programming_error():
    with pytest.raises(ValueError):
        SecurityIngestionService(
            fetcher=FakeFetcher(),
            store=FakeStore(),
            collectors=[],
            clock=lambda: datetime.now(UTC),
            pause=lambda: None,  # type: ignore[arg-type,return-value]
            backoff=lambda attempt: None,  # type: ignore[arg-type,return-value]
        )


async def test_request_registers_a_pending_batch_and_run_executes_it_once():
    fetcher, store = FakeFetcher(), FakeStore()
    svc = service(fetcher, store)
    ingestion_id = await svc.request("600009")
    assert store.batches[ingestion_id]["status"] is IngestionStatus.PENDING
    assert fetcher.requests == []
    summary = await svc.run(ingestion_id)
    assert summary.ingestion_id == ingestion_id
    assert summary.status is IngestionStatus.SUCCEEDED
    with pytest.raises(IngestionNotRunnable):  # 同一批次重新投递不会再跑一遍
        await svc.run(ingestion_id)
    assert len(fetcher.requests) == 21


async def test_run_refuses_an_unknown_batch():
    with pytest.raises(IngestionNotRunnable):
        await service(FakeFetcher(), FakeStore()).run("no-such-batch")


# ---------------------------------------------------------------- 重试


@pytest.mark.parametrize(
    "code",
    [
        "crawl_deadline_exceeded",
        "crawl_transport_failed",
        "crawl_content_encoding_invalid",
        "crawl_dns_failed",
    ],
)
async def test_transient_fetch_errors_are_retried_and_the_count_is_recorded(code):
    fetcher = FakeFetcher({"lrbAjaxNew": [FetchFailed(code), FetchFailed(code)]})
    store, backoffs = FakeStore(), []
    summary = await service(fetcher, store, backoffs=backoffs).ingest("600009")
    assert summary.status is IngestionStatus.SUCCEEDED and summary.items == 21
    assert backoffs == [1, 2]
    assert len(fetcher.requests) == 23
    retried = [i for _, i in store.items if i.meta.get("attempts")]
    assert [(i.kind, i.meta["attempts"]) for i in retried] == [
        (ItemKind.STATEMENT_DATA, 3)
    ]


async def test_a_busy_source_is_retried_and_only_the_last_response_is_kept():
    busy = RawResponse(503, "text/html", b"busy", "x")
    fetcher = FakeFetcher({"zcfzbDateAjaxNew": [busy]})
    store, backoffs = FakeStore(), []
    summary = await service(fetcher, store, backoffs=backoffs).ingest("600009")
    assert summary.status is IngestionStatus.SUCCEEDED and summary.items == 21
    assert backoffs == [1] and b"busy" not in store.objects.values()
    assert all(i.http_status == 200 for _, i in store.items)


async def test_retries_stop_after_three_attempts():
    fetcher = FakeFetcher({"szse_stock.json": FetchFailed("crawl_transport_failed")})
    store, backoffs = FakeStore(), []
    summary = await service(fetcher, store, backoffs=backoffs).ingest("600009")
    assert summary.status is IngestionStatus.FAILED
    assert summary.error_detail == "crawl_transport_failed"
    assert len(fetcher.requests) == 3 and backoffs == [1, 2]


@pytest.mark.parametrize(
    "code",
    [
        "crawl_url_forbidden",
        "crawl_address_forbidden",
        "crawl_response_too_large",
        "crawl_redirect_forbidden",
        "crawl_tls_downgrade_forbidden",
        "crawl_content_encoding_forbidden",
    ],
)
async def test_errors_that_will_not_heal_are_not_retried(code):
    fetcher = FakeFetcher({"szse_stock.json": FetchFailed(code)})
    backoffs: list[int] = []
    summary = await service(fetcher, FakeStore(), backoffs=backoffs).ingest("600009")
    assert (summary.status, summary.error_detail) == (IngestionStatus.FAILED, code)
    assert len(fetcher.requests) == 1 and backoffs == []


@pytest.mark.parametrize("status", [400, 403, 404])
async def test_client_errors_are_not_retried(status):
    fetcher = FakeFetcher(
        {"szse_stock.json": RawResponse(status, "text/html", b"no", "x")}
    )
    backoffs: list[int] = []
    summary = await service(fetcher, FakeStore(), backoffs=backoffs).ingest("600009")
    assert (summary.error_code, summary.error_detail) == ("http_status", str(status))
    assert len(fetcher.requests) == 1 and backoffs == []
