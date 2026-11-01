"""测试 Wind 动态补采任务（WP-5）。"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.tasks.wind_dynamic_task import execute_wind_fetch, wind_fetch_financials


@pytest.mark.anyio
async def test_execute_wind_fetch_replay_maotai_mock_delivery():
    """验证重放模式下获取茅台数据并模拟交付给 knowbase。"""
    mock_knowbase = AsyncMock()
    mock_knowbase.deliver_public.return_value = {
        "material_id": "mat-wind-600519",
        "current_version": 1,
    }

    res = await execute_wind_fetch(
        subject="600519.SH",
        fields=["IS.REVENUE", "CF.NET_OPERATING"],
        report_date="2024-12-31",
        replay_mode=True,
        knowbase_client=mock_knowbase,
    )

    assert res["status"] == "delivered"
    assert res["material_id"] == "mat-wind-600519"
    assert res["facts"]["IS.REVENUE"] == "170987192572.76"
    assert res["facts"]["CF.NET_OPERATING"] == "92442166423.51"
    mock_knowbase.deliver_public.assert_called_once()


@pytest.mark.anyio
async def test_execute_wind_fetch_user_beneficiaries_delivery():
    """验证带受益人时的采集原件交付路径。"""
    mock_knowbase = AsyncMock()
    mock_knowbase.deliver_collected.return_value = [
        {"subject": "alice", "material_id": "mat-user-1", "version_no": 1}
    ]

    res = await execute_wind_fetch(
        subject="300750.SZ",
        fields=["IS.REVENUE", "IS.RND"],
        report_date="2024-06-30",
        beneficiaries=[{"subject": "alice", "item_ref": "ref-1", "basis": "demand"}],
        replay_mode=True,
        knowbase_client=mock_knowbase,
    )

    assert res["status"] == "delivered"
    assert res["material_id"] == "mat-user-1"
    assert res["facts"]["IS.REVENUE"] == "166767419000.00"
    mock_knowbase.deliver_collected.assert_called_once()


@pytest.mark.anyio
async def test_execute_wind_fetch_gap_when_unmatched():
    """数据源不可用时返回 gap 缺口说明，业务层不抛未捕获崩溃。"""
    res = await execute_wind_fetch(
        subject="000001.SZ",
        fields=["IS.REVENUE"],
        report_date="2026-12-31",  # 未披露或未收录
        replay_mode=True,
    )
    assert res["status"] == "gap"
    assert res["reason"] == "data_not_available_in_source"
    assert res["facts"] == {}


def test_wind_fetch_financials_celery_entrypoint():
    """验证 Celery 同步任务入口。"""
    res = wind_fetch_financials(
        subject="600519.SH",
        fields=["IS.REVENUE"],
        report_date="2024-12-31",
        replay_mode=True,
    )
    assert res["status"] == "fetched"
    assert res["facts"]["IS.REVENUE"] == "170987192572.76"
