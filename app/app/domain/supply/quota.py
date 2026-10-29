"""需求的配额（SDD 0045 第 2.4 节第 4 条，块 4）：请求频率、在途单数、订阅条数、每人每月
新取份数，都按归属用户算（平台人员用的账号也是用户，同一套数）。

只有真正的新受理才扣：同一幂等钥匙回原结果的不算，「再试一次」用新的钥匙算新的。
受理时没有渠道、当场「无法满足」的项不算新取。

数 0045 没写，这里是拟值，部署可以用环境变量改（`core.config` 的 `supply_quota_*`）；
交回报告里列出请 Opus 定。用户空间那一条只对非公开的内容有意义：公开披露是第二区的
引用，不计入用户空间、只受代采配额限制（数据拓扑第十五节），现在的渠道都是公开披露。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.domain.supply.models import SupplyError

BEIJING = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class Quota:
    per_hour: int = 60  # 每人每小时最多提几张新单
    open_demands: int = 20  # 每人同时最多几张没办完的单
    monthly_items: int = 500  # 每人每月最多新取几份（按北京时间的自然月）
    subscriptions: int = 20  # 每人最多几条生效中的订阅（订阅随块 9 接上时核）


@dataclass(frozen=True)
class Usage:
    """这位用户眼下用了多少（受理前在同一个事务里数）。"""

    last_hour: int
    open_demands: int
    this_month: int


def month_start(now: datetime) -> datetime:
    local = now.astimezone(BEIJING)
    return local.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def hour_ago(now: datetime) -> datetime:
    return now - timedelta(hours=1)


def check_quota(quota: Quota, usage: Usage, new_items: int) -> None:
    """超了就拒（需求方看到「已拒绝」和一句原因，如「本月配额用完」）。"""
    if usage.last_hour >= quota.per_hour:
        raise SupplyError(429, "quota_rate", "too many demands in the last hour")
    if usage.open_demands >= quota.open_demands:
        raise SupplyError(429, "quota_open_demands", "too many demands are still open")
    if new_items and usage.this_month + new_items > quota.monthly_items:
        raise SupplyError(
            429, "quota_monthly", "this month's quota of new documents is used up"
        )
