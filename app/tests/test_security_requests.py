"""采集申请的规则（0008-info-intake、PRD/apps/info.md 的 AT-INFO-*）。不碰数据库。"""

from __future__ import annotations

import pytest
from security_requests_support import ALICE, BOB, OWNER, build, dataset

from app.domain.cross_app import clean_origin
from app.domain.securities.requests import (
    SYSTEM_WATCHLIST,
    DatasetState,
    IngestionState,
    Progress,
    RequestError,
    RequestKind,
    RequestStatus,
    clean_reason,
    progress_after_approval,
)

KNOWN = frozenset({"investment", "knowledge"})


# ---------------------------------------------------------------- 进度怎么推
@pytest.mark.parametrize(
    ("ingestion", "data", "expected"),
    [
        (None, None, Progress.QUEUED),
        (IngestionState("pending"), None, Progress.QUEUED),
        (IngestionState("running"), None, Progress.COLLECTING),
        (IngestionState("failed", error_code="fetch_failed"), None, Progress.FAILED),
        (IngestionState("succeeded"), None, Progress.BUILDING),
        (
            IngestionState("succeeded", build_error="statement_missing"),
            None,
            Progress.FAILED,
        ),
        (
            IngestionState("succeeded"),
            dataset(status="quality_failed", registered=False),
            Progress.QUALITY_FAILED,
        ),
        (IngestionState("succeeded"), dataset(registered=False), Progress.REGISTERING),
        (
            IngestionState("succeeded"),
            dataset(registered=False, registration_error="dataset_rejected"),
            Progress.REGISTRATION_FAILED,
        ),
        (IngestionState("succeeded"), dataset(), Progress.AVAILABLE),
    ],
)
def test_progress_after_approval(ingestion, data, expected):
    assert (
        progress_after_approval(ingestion, data, registration_enabled=True) is expected
    )


def test_without_a_downstream_a_published_dataset_is_already_available():
    progress = progress_after_approval(
        IngestionState("succeeded"),
        dataset(registered=False),
        registration_enabled=False,
    )
    assert progress is Progress.AVAILABLE


# ---------------------------------------------------------------- 链接带来的参数
@pytest.mark.parametrize(
    ("app", "ref", "expected"),
    [
        ("investment", "task:1f0c", ("investment", "task:1f0c")),
        ("investment", None, ("investment", None)),
        ("investment", "has space", ("investment", None)),
        ("investment", "a" * 129, ("investment", None)),
        ("investment", "https://evil.example.test", ("investment", None)),
        ("unknown", "x", None),
        ("Investment", "x", None),
        ("", "x", None),
        (None, "x", None),
    ],
)
def test_origin_from_a_link_is_never_trusted(app, ref, expected):
    origin = clean_origin(app, ref, known_apps=KNOWN)
    assert (origin and (origin.app, origin.ref)) == expected


def test_reason_is_trimmed_bounded_and_printable():
    assert clean_reason(None) is None
    assert clean_reason("   ") is None
    assert clean_reason("  要看 2025 年报  ") == "要看 2025 年报"
    assert clean_reason("第一行\n第二行") == "第一行\n第二行"
    with pytest.raises(RequestError) as too_long:
        clean_reason("字" * 501)
    assert too_long.value.code == "reason_too_long"
    with pytest.raises(RequestError) as invalid:
        clean_reason("a\x00b")
    assert invalid.value.code == "reason_invalid"


# ---------------------------------------------------------------- 提交
async def test_a_company_outside_the_watchlist_waits_for_approval():
    """AT-INFO-01"""
    service, store, _ = build()
    view = await service.submit(
        "600519",
        actor_id=ALICE,
        reason="要看毛利率",
        origin_app="investment",
        origin_ref="task:42",
    )
    assert view.progress is Progress.PENDING
    assert view.request.kind is RequestKind.INITIAL
    assert view.request.status is RequestStatus.PENDING
    assert store.started == []  # 没有开始采集
    mine = view.request.requester(ALICE)
    assert mine.reason == "要看毛利率"
    assert (mine.origin.app, mine.origin.ref) == ("investment", "task:42")
    assert store.commits == 1 and store.locked == [ALICE]


async def test_a_company_on_the_watchlist_is_approved_by_the_system():
    """AT-INFO-03"""
    service, store, _ = build("600519")
    view = await service.submit("600519", actor_id=ALICE)
    assert view.progress is Progress.QUEUED
    assert view.request.status is RequestStatus.APPROVED
    assert view.request.decided_by == SYSTEM_WATCHLIST
    assert store.started == ["600519"]
    assert view.request.ingestion_id in store.ingestions


@pytest.mark.parametrize("code", ["60051", "abc123", "900901", "", "6005190"])
async def test_codes_that_are_not_a_share_codes_are_refused(code):
    service, store, _ = build()
    with pytest.raises(RequestError) as error:
        await service.submit(code, actor_id=ALICE)
    assert error.value.code == "security_code_invalid"
    assert store.requests == {} and store.commits == 0


async def test_a_second_person_joins_the_same_request():
    """AT-INFO-05"""
    service, store, _ = build("600519")
    first = await service.submit("600519", actor_id=ALICE)
    second = await service.submit("600519", actor_id=BOB, reason="我也要")
    assert second.request.request_id == first.request.request_id
    assert second.request.active_requesters == 2
    assert store.started == ["600519"]  # 没有第二次采集
    assert len(store.requests) == 1


async def test_submitting_twice_returns_the_same_request():
    service, store, _ = build()
    first = await service.submit("600519", actor_id=ALICE, reason="第一次")
    again = await service.submit("600519", actor_id=ALICE, reason="第二次")
    assert again.request.request_id == first.request.request_id
    assert again.request.requester(ALICE).reason == "第一次"
    assert again.request.active_requesters == 1


async def test_losing_the_race_to_create_means_joining_the_winner():
    service, store, _ = build()
    winner = (await service.submit("600519", actor_id=BOB)).request
    store.requests.clear()
    store.race = winner  # 查的时候还没有，建的时候别人已经建了
    view = await service.submit("600519", actor_id=ALICE)
    assert view.request.request_id == winner.request_id
    assert view.request.active_requesters == 2


async def test_no_more_than_five_unfinished_requests_per_person():
    """AT-INFO-08"""
    service, store, _ = build()
    for code in ("600519", "000858", "600276", "002415", "600900"):
        await service.submit(code, actor_id=ALICE)
    with pytest.raises(RequestError) as error:
        await service.submit("601888", actor_id=ALICE)
    assert error.value.code == "too_many_open_requests"
    assert len(store.requests) == 5
    # 别人的名额不受影响；别人建的申请，名额满了的人也加不进去
    await service.submit("601888", actor_id=BOB)
    with pytest.raises(RequestError):
        await service.submit("601888", actor_id=ALICE)


async def test_a_finished_request_frees_a_slot():
    service, store, _ = build("600519", max_open=1)
    first = await service.submit("600519", actor_id=ALICE)
    ingestion_id = first.request.ingestion_id
    store.ingestions[ingestion_id] = IngestionState("succeeded")
    store.datasets[ingestion_id] = dataset()
    view = await service.submit("000858", actor_id=ALICE)
    assert view.progress is Progress.PENDING
    closed = store.requests[first.request.request_id]
    assert not closed.open and closed.outcome is Progress.AVAILABLE


async def test_a_company_with_data_gets_a_refresh_request():
    """AT-INFO-09 的后一半：已有数据时提交的是「申请更新」。"""
    service, store, _ = build()
    store.published["600519"] = dataset()
    view = await service.submit("600519", actor_id=ALICE)
    assert view.request.kind is RequestKind.REFRESH
    assert view.progress is Progress.PENDING


# ---------------------------------------------------------------- 查情况
async def test_availability_reports_data_open_request_and_room():
    """AT-INFO-09 的前一半"""
    service, store, _ = build("600519")
    nothing = await service.availability("000858", actor_id=ALICE)
    assert nothing.dataset is None and nothing.open_request is None
    assert (nothing.in_watchlist, nothing.mine, nothing.my_open_count) == (
        False,
        False,
        0,
    )
    assert nothing.market == "SZ" and nothing.max_open == 5

    store.published["600519"] = dataset("sh600519-financials-bbbb")
    await service.submit("600519", actor_id=BOB)
    seen = await service.availability("600519", actor_id=ALICE)
    assert seen.dataset.data_version == "sh600519-financials-bbbb"
    assert seen.in_watchlist is True
    assert seen.open_request.progress is Progress.QUEUED
    assert seen.mine is False and seen.my_open_count == 0
    assert (await service.availability("600519", actor_id=BOB)).mine is True


async def test_availability_does_not_create_anything():
    service, store, _ = build()
    await service.availability("600519", actor_id=ALICE)
    assert store.requests == {} and store.started == []


# ---------------------------------------------------------------- 撤回
async def test_one_of_two_withdraws_and_the_request_stays():
    """AT-INFO-06"""
    service, store, _ = build()
    first = await service.submit("600519", actor_id=ALICE)
    await service.submit("600519", actor_id=BOB)
    view = await service.withdraw(first.request.request_id, actor_id=ALICE)
    assert view.progress is Progress.PENDING and view.request.open
    assert view.request.active_requesters == 1
    assert view.request.requester(ALICE).withdrawn_at is not None


async def test_the_last_one_withdraws_and_the_request_closes():
    """AT-INFO-07"""
    service, store, _ = build()
    first = await service.submit("600519", actor_id=ALICE)
    view = await service.withdraw(first.request.request_id, actor_id=ALICE)
    assert view.progress is Progress.WITHDRAWN
    assert view.request.status is RequestStatus.WITHDRAWN and not view.request.open
    assert await service.listing(status=RequestStatus.PENDING, open_only=True) == []
    # 撤回之后可以再提，是一个新的申请
    again = await service.submit("600519", actor_id=ALICE)
    assert again.request.request_id != first.request.request_id


async def test_withdrawing_after_approval_is_refused():
    service, store, _ = build("600519")
    view = await service.submit("600519", actor_id=ALICE)
    with pytest.raises(RequestError) as error:
        await service.withdraw(view.request.request_id, actor_id=ALICE)
    assert error.value.code == "request_not_withdrawable"


async def test_nobody_can_withdraw_or_read_for_somebody_else():
    """AT-INFO-16"""
    service, store, _ = build()
    view = await service.submit("600519", actor_id=ALICE)
    for call in (service.withdraw, service.one_of_mine):
        with pytest.raises(RequestError) as error:
            await call(view.request.request_id, actor_id=BOB)
        assert error.value.code == "request_not_found"
    assert await service.mine(actor_id=BOB) == []
    assert [v.request.request_id for v in await service.mine(actor_id=ALICE)] == [
        view.request.request_id
    ]


# ---------------------------------------------------------------- 所有者
async def test_approval_starts_the_ingestion_and_progress_follows_it():
    """AT-INFO-02"""
    service, store, _ = build()
    pending = await service.submit("600519", actor_id=ALICE)
    request_id = pending.request.request_id
    approved = await service.approve(request_id, by=OWNER)
    assert approved.progress is Progress.QUEUED
    assert approved.request.decided_by == OWNER and store.started == ["600519"]
    ingestion_id = approved.request.ingestion_id

    async def seen() -> Progress:
        return (await service.one_of_mine(request_id, actor_id=ALICE)).progress

    store.ingestions[ingestion_id] = IngestionState("running")
    assert await seen() is Progress.COLLECTING
    store.ingestions[ingestion_id] = IngestionState("succeeded")
    assert await seen() is Progress.BUILDING
    store.datasets[ingestion_id] = dataset(registered=False)
    assert await seen() is Progress.REGISTERING
    assert store.requests[request_id].open
    store.datasets[ingestion_id] = dataset()
    view = await service.one_of_mine(request_id, actor_id=ALICE)
    assert view.progress is Progress.AVAILABLE
    assert view.dataset.end_date == "2025-12-31"
    assert not store.requests[request_id].open


async def test_the_outcome_is_fixed_once_the_request_is_closed():
    service, store, _ = build("600519")
    view = await service.submit("600519", actor_id=ALICE)
    ingestion_id = view.request.ingestion_id
    store.ingestions[ingestion_id] = IngestionState("failed", error_code="fetch_failed")
    request_id = view.request.request_id
    assert (
        await service.one_of_mine(request_id, actor_id=ALICE)
    ).progress is Progress.FAILED
    store.ingestions[ingestion_id] = IngestionState("succeeded")  # 之后再怎么变
    store.datasets[ingestion_id] = dataset()
    assert (
        await service.one_of_mine(request_id, actor_id=ALICE)
    ).progress is Progress.FAILED


async def test_quality_failure_is_reported_as_such():
    """AT-INFO-10"""
    service, store, _ = build("600519")
    view = await service.submit("600519", actor_id=ALICE)
    ingestion_id = view.request.ingestion_id
    store.ingestions[ingestion_id] = IngestionState("succeeded")
    store.datasets[ingestion_id] = DatasetState(
        status="quality_failed",
        dataset_id="sh600519-financials",
        data_version="sh600519-financials-cccc",
        start_date="2016-12-31",
        end_date="2025-12-31",
        registered=False,
    )
    seen = await service.one_of_mine(view.request.request_id, actor_id=ALICE)
    assert seen.progress is Progress.QUALITY_FAILED and seen.dataset is None


async def test_rejection_needs_a_reason_and_closes_the_request():
    """AT-INFO-04"""
    service, store, _ = build()
    view = await service.submit("600519", actor_id=ALICE)
    request_id = view.request.request_id
    for note, code in [
        ("", "rejection_note_required"),
        ("   ", "rejection_note_required"),
        ("字" * 501, "rejection_note_too_long"),
    ]:
        with pytest.raises(RequestError) as error:
            await service.reject(request_id, by=OWNER, note=note)
        assert error.value.code == code
    rejected = await service.reject(request_id, by=OWNER, note=" 存储不够，下个月再说 ")
    assert rejected.progress is Progress.REJECTED
    assert rejected.request.decision_note == "存储不够，下个月再说"
    assert not rejected.request.open and store.started == []


async def test_a_request_is_decided_only_once():
    service, store, _ = build()
    view = await service.submit("600519", actor_id=ALICE)
    request_id = view.request.request_id
    await service.approve(request_id, by=OWNER)
    for call in (
        lambda: service.approve(request_id, by=OWNER),
        lambda: service.reject(request_id, by=OWNER, note="晚了"),
    ):
        with pytest.raises(RequestError) as error:
            await call()
        assert error.value.code == "request_not_pending"
    assert store.started == ["600519"]
    with pytest.raises(RequestError) as missing:
        await service.approve("no-such-request", by=OWNER)
    assert missing.value.code == "request_not_found"


async def test_approving_can_add_the_company_to_the_watchlist():
    service, store, watchlist = build()
    view = await service.submit("600519", actor_id=ALICE)
    await service.approve(view.request.request_id, by=OWNER, add_to_watchlist=True)
    entries = await service.watchlist()
    assert [(e.security_code, e.added_by) for e in entries] == [("600519", OWNER)]


async def test_nothing_is_added_to_the_watchlist_on_its_own():
    """P7 还没有定：入库之后不自动进关注清单。"""
    service, store, watchlist = build()
    view = await service.submit("600519", actor_id=ALICE)
    approved = await service.approve(view.request.request_id, by=OWNER)
    store.ingestions[approved.request.ingestion_id] = IngestionState("succeeded")
    store.datasets[approved.request.ingestion_id] = dataset()
    await service.one_of_mine(view.request.request_id, actor_id=ALICE)
    assert await service.watchlist() == []


async def test_listing_for_the_owner():
    service, store, _ = build("600900")
    await service.submit("600519", actor_id=ALICE)
    await service.submit("000858", actor_id=BOB)
    await service.submit("600900", actor_id=BOB)
    pending = await service.listing(status=RequestStatus.PENDING, open_only=True)
    assert [v.request.security_code for v in pending] == ["600519", "000858"]
    assert len(await service.listing()) == 3


# ---------------------------------------------------------------- 关注清单
async def test_watchlist_changes_leave_a_trace():
    service, store, watchlist = build("600009")
    assert await service.watch("600519", by=OWNER, note="白酒") is True
    assert await service.watch("600519", by=OWNER, note=None) is False
    assert await service.unwatch("600009", by=OWNER, note="不看了") is True
    assert await service.unwatch("600009", by=OWNER, note=None) is False
    assert [e.security_code for e in await service.watchlist()] == ["600519"]
    everything = await service.watchlist(include_removed=True)
    removed = next(e for e in everything if e.security_code == "600009")
    assert (removed.removed_by, removed.removal_note) == (OWNER, "不看了")
    with pytest.raises(RequestError):
        await service.watch("abc", by=OWNER, note=None)


# ---------------------------------------------------------------- 配置
def test_request_settings():
    """谁可以把用户带到申请页、各自的回跳地址，是跨应用跳转的配置（tests/test_cross_app.py）。"""
    from core.config import Settings

    blank = Settings(_env_file=None)
    assert blank.security_request_max_open == 5
    assert set(blank.cross_app_sources()) == {"investment", "knowledge"}
