"""测试 Wind 适配器脱敏骨架、科目代码映射与重放模式（WP-4）。"""

from __future__ import annotations

import pytest

from app.infrastructure.external.wind_adapter import (
    CHART_TO_WIND,
    WIND_TO_CHART,
    WindAdapter,
    WindAuthError,
    WindCredential,
)


def test_wind_to_chart_mappings_bidirectional():
    """验证科目代码映射完整性。"""
    assert WIND_TO_CHART["tot_oper_rev"] == "IS.REVENUE"
    assert WIND_TO_CHART["net_profit_parent"] == "IS.NET_PROFIT_PARENT"
    assert WIND_TO_CHART["cash_pay_acq_const_fi_olta"] == "CF.CAPEX"
    assert CHART_TO_WIND["IS.REVENUE"] in ("tot_oper_rev", "oper_rev")
    assert CHART_TO_WIND["CF.CAPEX"] == "cash_pay_acq_const_fi_olta"


def test_wind_credential_security_masking():
    """凭据安全脱敏，repr 和 str 绝不泄露敏感明文。"""
    cred = WindCredential(
        api_key="WIND_SECRET_KEY_123456", api_secret="SUPER_SECRET_PAYLOAD"
    )
    text_repr = repr(cred)
    text_str = str(cred)
    assert "WIND_SECRET_KEY_123456" not in text_repr
    assert "SUPER_SECRET_PAYLOAD" not in text_repr
    assert "WIND***" in text_repr
    assert "secret_set=True" in text_repr
    assert text_str == text_repr


@pytest.mark.anyio
async def test_wind_adapter_replay_mode_maotai():
    """重放模式下获取贵州茅台 2024 年报事实数据。"""
    adapter = WindAdapter(replay_mode=True)
    res = await adapter.fetch_financials(
        subject="600519.SH",
        chart_fields=["IS.REVENUE", "IS.COST", "CF.NET_OPERATING", "CF.CAPEX"],
        report_date="2024-12-31",
    )
    assert res["matched"] is True
    assert res["mode"] == "replay"
    assert res["facts"]["IS.REVENUE"] == "170987192572.76"
    assert res["facts"]["IS.COST"] == "14142389864.71"
    assert res["facts"]["CF.NET_OPERATING"] == "92442166423.51"
    assert res["facts"]["CF.CAPEX"] == "5671234180.20"


@pytest.mark.anyio
async def test_wind_adapter_replay_mode_catl():
    """重放模式下获取宁德时代 2024 中报事实数据。"""
    adapter = WindAdapter(replay_mode=True)
    res = await adapter.fetch_financials(
        subject="300750.SZ",
        chart_fields=["IS.REVENUE", "IS.RND"],
        report_date="2024-06-30",
    )
    assert res["matched"] is True
    assert res["facts"]["IS.REVENUE"] == "166767419000.00"
    assert res["facts"]["IS.RND"] == "8591720000.00"


@pytest.mark.anyio
async def test_wind_adapter_unconfigured_auth_error():
    """未配置凭据且未开启重放模式时抛出明确异常，绝不发起非受控外网请求。"""
    adapter = WindAdapter(credential=WindCredential(api_key=None), replay_mode=False)
    with pytest.raises(WindAuthError):
        await adapter.fetch_financials(
            subject="600519.SH",
            chart_fields=["IS.REVENUE"],
            report_date="2024-12-31",
        )


@pytest.mark.anyio
async def test_safe_fetch_success_and_partial_gaps():
    """WP-7: 测试正常取数与部分科目缺失时的降级响应。"""
    from app.infrastructure.external.wind_adapter import (
        CircuitBreaker,
        DegradationCode,
        safe_fetch_with_degradation,
    )

    adapter = WindAdapter(replay_mode=True)
    breaker = CircuitBreaker()

    # 完整命中
    res = await safe_fetch_with_degradation(
        adapter, breaker, "600519.SH", ["IS.REVENUE", "IS.COST"], "2024-12-31"
    )
    assert res["status"] == "success"
    assert res["code"] == DegradationCode.SUCCESS
    assert res["gaps"] == []
    assert not breaker.is_open

    # 部分缺失（包含一个不存在的科目）
    res_partial = await safe_fetch_with_degradation(
        adapter,
        breaker,
        "600519.SH",
        ["IS.REVENUE", "NON_EXISTING_METRIC"],
        "2024-12-31",
    )
    assert res_partial["status"] == "partial"
    assert res_partial["code"] == DegradationCode.PARTIAL_GAPS
    assert "NON_EXISTING_METRIC" in res_partial["gaps"]
    assert "IS.REVENUE" in res_partial["facts"]


@pytest.mark.anyio
async def test_safe_fetch_auth_failure_trips_circuit_breaker():
    """WP-7: 401 认证失败立即触发断路器熔断，避免无效重试。"""
    from app.infrastructure.external.wind_adapter import (
        CircuitBreaker,
        DegradationCode,
        safe_fetch_with_degradation,
    )

    # 真实模式且未配 Token
    adapter = WindAdapter(credential=WindCredential(api_key=None), replay_mode=False)
    breaker = CircuitBreaker()

    # 首次触发 401
    res1 = await safe_fetch_with_degradation(
        adapter, breaker, "600519.SH", ["IS.REVENUE"], "2024-12-31"
    )
    assert res1["status"] == "degraded"
    assert res1["code"] == DegradationCode.AUTH_FAILED
    assert breaker.is_open is True

    # 再次请求时断路器已处于 OPEN 状态，直接阻断
    res2 = await safe_fetch_with_degradation(
        adapter, breaker, "600519.SH", ["IS.REVENUE"], "2024-12-31"
    )
    assert res2["status"] == "degraded"
    assert "Circuit breaker is OPEN" in res2["message"]


@pytest.mark.anyio
async def test_safe_fetch_no_data_disclosed():
    """WP-7: 未披露期间返回明确 no_data_disclosed 说明。"""
    from app.infrastructure.external.wind_adapter import (
        CircuitBreaker,
        DegradationCode,
        safe_fetch_with_degradation,
    )

    adapter = WindAdapter(replay_mode=True)
    breaker = CircuitBreaker()

    res = await safe_fetch_with_degradation(
        adapter, breaker, "600519.SH", ["IS.REVENUE"], "2029-12-31"
    )
    assert res["status"] == "degraded"
    assert res["code"] == DegradationCode.NO_DATA_DISCLOSED
    assert res["facts"] == {}
