"""Keep the original cancellation scenario, injecting the lost historical timing."""

from test_crawl_concurrency_db import (
    test_inflight_worker_blocks_same_host_not_other_host_and_cancel_releases as scenario,
)
from test_delivery_clock_regression_db import shift_first_release
from test_durable_delivery_db import db as db


async def test_busy_source_release_clock_regression_does_not_block_retry(
    db, monkeypatch
):
    # The first release belongs to the rejected same-host message. Its timestamp
    # used to become a future active lease, even after the source lock was freed.
    releases = shift_first_release(db, monkeypatch)
    await scenario(db, monkeypatch)
    assert len(releases) >= 4
