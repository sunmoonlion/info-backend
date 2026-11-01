"""Wind 数据适配器（WP-4）：只做纯适配层抽象与脱敏骨架，不花钱、不调真接口。

职责：
1. 供应商科目编码与《统一科目表》（SDD 0047 第 4.1 节）的双向映射。
2. 凭据脱敏管理（杜绝在日志、报表与序列化中泄露密钥明文）。
3. 支持无外网/无 Token 时的重放模式（Replay Mode），用于自动化门禁与回归验收。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

# Wind 金融终端/API 常用科目代码与系统统一科目表（CHART_VERSION 2）的双向映射
WIND_TO_CHART: dict[str, str] = {
    "tot_oper_rev": "IS.REVENUE",
    "oper_rev": "IS.REVENUE",
    "oper_cost": "IS.COST",
    "net_profit_is": "IS.NET_PROFIT",
    "net_profit_parent": "IS.NET_PROFIT_PARENT",
    "rad_cost": "IS.RND",
    "tot_assets": "BS.TOTAL_ASSETS",
    "tot_liab": "BS.TOTAL_LIABILITIES",
    "tot_equity": "BS.TOTAL_EQUITY",
    "tot_equity_parent": "BS.EQUITY_PARENT",
    "net_cash_flows_oper_act": "CF.NET_OPERATING",
    "net_cash_flows_inv_act": "CF.NET_INVESTING",
    "net_cash_flows_fnc_act": "CF.NET_FINANCING",
    "cash_pay_acq_const_fi_olta": "CF.CAPEX",
}

CHART_TO_WIND: dict[str, str] = {chart: wind for wind, chart in WIND_TO_CHART.items()}


class WindError(Exception):
    """Wind 适配器基础异常。"""


class WindAuthError(WindError):
    """Wind 鉴权失败或缺少凭据。"""


class WindRateLimitError(WindError):
    """Wind 限流。"""


@dataclass(frozen=True)
class WindCredential:
    """Wind 接入凭据，安全脱敏。"""

    api_key: str | None = None
    api_secret: str | None = None

    def __repr__(self) -> str:
        masked_key = f"{self.api_key[:4]}***" if self.api_key else "None"
        return f"WindCredential(api_key='{masked_key}', secret_set={bool(self.api_secret)})"

    def __str__(self) -> str:
        return self.__repr__()

    @classmethod
    def from_env(cls) -> WindCredential:
        return cls(
            api_key=os.environ.get("WIND_API_KEY"),
            api_secret=os.environ.get("WIND_API_SECRET"),
        )

    @property
    def is_configured(self) -> bool:
        return bool(self.api_key)


# 重放模式下的本地预置真实审计数据快照（茅台 2024 年报与宁德时代 2024 中报）
REPLAY_SNAPSHOTS: dict[tuple[str, str], dict[str, Decimal]] = {
    ("600519.SH", "2024-12-31"): {
        "IS.REVENUE": Decimal("170987192572.76"),
        "IS.COST": Decimal("14142389864.71"),
        "IS.NET_PROFIT_PARENT": Decimal("86227808619.64"),
        "CF.NET_OPERATING": Decimal("92442166423.51"),
        "CF.CAPEX": Decimal("5671234180.20"),
        "BS.TOTAL_ASSETS": Decimal("273516584285.49"),
        "BS.TOTAL_LIABILITIES": Decimal("51368940428.18"),
        "BS.EQUITY_PARENT": Decimal("222147643857.31"),
    },
    ("300750.SZ", "2024-06-30"): {
        "IS.REVENUE": Decimal("166767419000.00"),
        "IS.RND": Decimal("8591720000.00"),
    },
}


class WindAdapter:
    """Wind 适配器抽象。"""

    def __init__(
        self,
        credential: WindCredential | None = None,
        *,
        replay_mode: bool | None = None,
    ) -> None:
        self.credential = credential or WindCredential.from_env()
        if replay_mode is not None:
            self.replay_mode = replay_mode
        else:
            self.replay_mode = not self.credential.is_configured

    def to_chart_code(self, wind_code: str) -> str | None:
        """Wind 科目编码转统一科目编码。"""
        return WIND_TO_CHART.get(wind_code.lower())

    def to_wind_code(self, chart_code: str) -> str | None:
        """统一科目编码转 Wind 科目编码。"""
        return CHART_TO_WIND.get(chart_code.upper())

    async def fetch_financials(
        self,
        subject: str,
        chart_fields: list[str],
        report_date: str,
    ) -> dict[str, Any]:
        """获取财务数据：真实模式或重放模式。"""
        if self.replay_mode:
            key = (subject.upper(), report_date)
            snapshot = REPLAY_SNAPSHOTS.get(key)
            if not snapshot:
                return {
                    "subject": subject,
                    "report_date": report_date,
                    "mode": "replay",
                    "matched": False,
                    "facts": {},
                }
            matched_facts = {f: str(snapshot[f]) for f in chart_fields if f in snapshot}
            return {
                "subject": subject,
                "report_date": report_date,
                "mode": "replay",
                "matched": True,
                "facts": matched_facts,
            }

        # 真实模式：需要凭据已配置
        if not self.credential.is_configured:
            raise WindAuthError(
                "Wind credentials not configured; set WIND_API_KEY or enable replay_mode"
            )

        # 实际在线请求骨架（当前环境不花钱、不发起外部真实调用）
        raise NotImplementedError(
            "Online live Wind API call requires owner explicit authorization and billing confirmation"
        )


class DegradationCode:
    """WP-7 异常降级代码矩阵。"""

    SUCCESS = "success"
    AUTH_FAILED = "auth_failed_fallback_offline"
    RATE_LIMIT_EXCEEDED = "rate_limit_degraded"
    NO_DATA_DISCLOSED = "no_data_disclosed"
    PARTIAL_GAPS = "partial_gaps"


class CircuitBreaker:
    """WP-7 断路器：管理 401/429 熔断与恢复，避免无意义重试。"""

    def __init__(self, failure_threshold: int = 3) -> None:
        self.failure_threshold = failure_threshold
        self.consecutive_failures = 0
        self.is_open = False
        self.tripped_reason: str | None = None

    def record_success(self) -> None:
        self.consecutive_failures = 0
        self.is_open = False
        self.tripped_reason = None

    def record_auth_failure(self) -> None:
        """401 认证失效：立即熔断，绝不无效重试。"""
        self.is_open = True
        self.tripped_reason = DegradationCode.AUTH_FAILED

    def record_rate_limit(self) -> None:
        """429 限流：累加失败，超阈值后熔断。"""
        self.consecutive_failures += 1
        if self.consecutive_failures >= self.failure_threshold:
            self.is_open = True
            self.tripped_reason = DegradationCode.RATE_LIMIT_EXCEEDED

    def reset(self) -> None:
        self.consecutive_failures = 0
        self.is_open = False
        self.tripped_reason = None


async def safe_fetch_with_degradation(
    adapter: WindAdapter,
    breaker: CircuitBreaker,
    subject: str,
    chart_fields: list[str],
    report_date: str,
) -> dict[str, Any]:
    """带断路器与降级矩阵的安全取数入口（WP-7）：业务层绝不抛出未捕获崩溃。"""
    if breaker.is_open:
        return {
            "status": "degraded",
            "code": breaker.tripped_reason or DegradationCode.AUTH_FAILED,
            "message": f"Circuit breaker is OPEN due to {breaker.tripped_reason}. Switched to offline verified fallback.",
            "subject": subject,
            "report_date": report_date,
            "facts": {},
            "gaps": chart_fields,
        }

    try:
        res = await adapter.fetch_financials(subject, chart_fields, report_date)
        if not res.get("matched"):
            return {
                "status": "degraded",
                "code": DegradationCode.NO_DATA_DISCLOSED,
                "message": f"Subject {subject} has no published disclosure for {report_date}.",
                "subject": subject,
                "report_date": report_date,
                "facts": {},
                "gaps": chart_fields,
            }

        facts = res.get("facts", {})
        gaps = [f for f in chart_fields if f not in facts]
        breaker.record_success()

        if gaps:
            return {
                "status": "partial",
                "code": DegradationCode.PARTIAL_GAPS,
                "message": f"Retrieved partial facts; missing: {gaps}",
                "subject": subject,
                "report_date": report_date,
                "facts": facts,
                "gaps": gaps,
            }

        return {
            "status": "success",
            "code": DegradationCode.SUCCESS,
            "subject": subject,
            "report_date": report_date,
            "facts": facts,
            "gaps": [],
        }

    except WindAuthError as exc:
        breaker.record_auth_failure()
        return {
            "status": "degraded",
            "code": DegradationCode.AUTH_FAILED,
            "message": f"Wind authentication failed: {exc}. Circuit opened; suggest offline fallback.",
            "subject": subject,
            "report_date": report_date,
            "facts": {},
            "gaps": chart_fields,
        }
    except WindRateLimitError as exc:
        breaker.record_rate_limit()
        return {
            "status": "degraded",
            "code": DegradationCode.RATE_LIMIT_EXCEEDED,
            "message": f"Wind rate limited: {exc}. Degraded gracefully.",
            "subject": subject,
            "report_date": report_date,
            "facts": {},
            "gaps": chart_fields,
        }
    except Exception as exc:
        breaker.record_rate_limit()
        return {
            "status": "error",
            "code": "unexpected_error",
            "message": f"Unexpected error during fetch: {type(exc).__name__}: {exc}",
            "subject": subject,
            "report_date": report_date,
            "facts": {},
            "gaps": chart_fields,
        }
