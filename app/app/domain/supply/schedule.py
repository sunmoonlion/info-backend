"""调度与切合的规矩（SDD 0045 第 3.3 节、第四节；采集讨论记录第 7、8 条）。

**排队**：每个渠道一条队列，按登记的并发（同时最多跑几个作业）排出去；上传资料的加工
不问渠道，排在单独的加工队列里（`PROCESS_QUEUE`）。

**四档**（采集讨论记录第 7 条原话）：① 有任务在等数据的（如委托卡在「等数据」）；
② knowlab 测试集这类成批的；③ 订阅；④ 后台补加工。同一档里按用户轮流，「一个人一次要
50 份不挤掉别人」。共享的作业取等它的需求里最靠前的一档；加工作业属第 ④ 档（第 8 条第 4
小条）。

**限速**（0045 第 3.1 节「限速按出站地址全局算」）：每问一次渠道算一次，每分钟几次按渠道
登记的限速、也按出站地址的全局限速；每取回一份算一份，每天几份按渠道登记的日上限（按
北京时间的自然日）。等一会儿就能问的在作业里等；日上限用完的作业放回队列，第二天再排。

**供给条件**（0045 第四节）：确认时记下的供给条件快照里，knowfeed 核两样：许可档下限
（例如要随公开专家用，就要「可以经专家给别人用」及更宽的）、只用哪几条渠道（来源限制）。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.domain.supply.models import Channel, DescriptionKey, refused

BEIJING = ZoneInfo("Asia/Shanghai")

TIER_TASK = 1  # ① 有任务在等数据的
TIER_BATCH = 2  # ② knowlab 测试集这类成批的
TIER_SUBSCRIPTION = 3  # ③ 订阅
TIER_BACKGROUND = 4  # ④ 后台补加工
TIERS = (TIER_TASK, TIER_BATCH, TIER_SUBSCRIPTION, TIER_BACKGROUND)

# 加工队列：不是渠道（渠道编号以小写字母开头，这个名字登记不上）
PROCESS_QUEUE = "_processing"

# 许可档从窄到宽（knowbase 的三档）：只许本人用、可以经专家给别人用、可以出售
LICENSE_RANK: dict[str, int] = {"self": 0, "via_expert": 1, "sale": 2}

# 卡住的「在跑」（拟值）：排出去多久还没开始跑算投递丢了；一次尝试最多跑多久（大年报
# 下载 3 分钟、加工几分钟）。调度自动收回、管理端手动重排都按这两个数
LOST_AFTER = timedelta(minutes=15)
ATTEMPT_LIMIT = timedelta(hours=2)

# 作业里问渠道要等多久以内就原地等；更久的放回队列
INLINE_WAIT_SECONDS = 30.0

# 预计时间的粗算：一个作业大约问渠道几次、取回几份，加工要多久（拟值）
REQUESTS_PER_JOB = 6
FETCHES_PER_JOB = 1
PROCESS_SECONDS = 60


def demand_tier(proposer: str, app: str) -> int:
    """需求单排第几档：订阅生成的第 ③ 档；knowlab 提的（测试集，成批的）第 ② 档；其余
    （任务在等、用户亲手提）第 ① 档。"""
    if proposer == "subscription":
        return TIER_SUBSCRIPTION
    if app == "knowlab":
        return TIER_BATCH
    return TIER_TASK


@dataclass(frozen=True)
class Conditions:
    """供给条件快照里 knowfeed 核的两样：许可档下限、只用哪几条渠道（空 = 不限）。"""

    min_license: str | None = None
    channels: tuple[str, ...] = ()

    def allows(self, channel: Channel) -> bool:
        if self.channels and channel.id not in self.channels:
            return False
        if self.min_license is None:
            return True
        return LICENSE_RANK.get(channel.license, -1) >= LICENSE_RANK[self.min_license]

    def as_dict(self) -> dict[str, object]:
        out: dict[str, object] = {}
        if self.min_license is not None:
            out["min_license"] = self.min_license
        if self.channels:
            out["channels"] = list(self.channels)
        return out

    @staticmethod
    def from_dict(data: object) -> Conditions:
        """读 `{"min_license": ..., "channels": [...]}`；写错的拒绝（不悄悄放宽）。"""
        if data is None:
            return Conditions()
        if not isinstance(data, dict):
            raise refused("supply_conditions_invalid", "conditions are an object")
        license_ = data.get("min_license")
        if license_ is not None and license_ not in LICENSE_RANK:
            raise refused(
                "supply_conditions_invalid", "min_license is self/via_expert/sale"
            )
        channels = data.get("channels") or []
        if not isinstance(channels, list) or not all(
            isinstance(c, str) and 0 < len(c) <= 40 for c in channels
        ):
            raise refused("supply_conditions_invalid", "channels are channel ids")
        return Conditions(license_, tuple(dict.fromkeys(channels)))

    @staticmethod
    def from_snapshot(snapshot: object) -> Conditions:
        """确认里的供给条件快照（knowbase 照存的 `supply`）：`conditions` 一栏。"""
        if not isinstance(snapshot, dict):
            return Conditions()
        return Conditions.from_dict(snapshot.get("conditions"))


def candidates(
    channels: list[Channel],
    key: DescriptionKey,
    conditions: Conditions,
    *,
    skip: tuple[str, ...] = (),
) -> tuple[list[Channel], list[Channel]]:
    """（能供这条描述的渠道，其中满足供给条件的），都按先后排；`skip` 是试过的。"""
    covering = [c for c in channels if c.covers(key) and c.id not in skip]
    covering.sort(key=lambda c: (c.priority, c.id))
    return covering, [c for c in covering if conditions.allows(c)]


# —— 按用户轮流 ——


@dataclass(frozen=True)
class Candidate:
    """排着的一个作业：第几档、什么时候建的、在等它的是哪些用户。"""

    job_id: str
    tier: int
    created_at: datetime
    owners: tuple[str, ...]


def pick(
    queued: list[Candidate],
    slots: int,
    served: dict[str, datetime],
    now: datetime,
) -> list[tuple[str, str]]:
    """从一条队列里挑 `slots` 个作业：先挑最靠前的一档；同档里挑等它的用户中最久没轮到
    的（从没轮到的最先），一样久的挑建得早的。回（作业，算轮到了谁）。"""
    turns = dict(served)
    pool = [c for c in queued if c.owners]
    out: list[tuple[str, str]] = []
    oldest = datetime.min.replace(tzinfo=now.tzinfo)

    def turn_of(c: Candidate) -> tuple[datetime, str]:
        owner = min(c.owners, key=lambda o: (turns.get(o, oldest), o))
        return turns.get(owner, oldest), owner

    while pool and len(out) < slots:
        tier = min(c.tier for c in pool)
        layer = [c for c in pool if c.tier == tier]
        best = min(layer, key=lambda c: (turn_of(c)[0], c.created_at, c.job_id))
        owner = turn_of(best)[1]
        out.append((best.job_id, owner))
        # 同一轮里挑下一个时，刚轮到的人排到最后
        turns[owner] = now + timedelta(microseconds=len(out))
        pool.remove(best)
    return out


# —— 限速 ——


class ChannelDeferred(Exception):  # noqa: N818 - 不是出错，是「过会儿再来」
    """渠道这会儿不能再问（日上限用完、或要等太久）：作业放回队列，`until` 以后再排。"""

    def __init__(self, until: datetime, reason: str) -> None:
        super().__init__(reason)
        self.until = until
        self.reason = reason


def day_start(now: datetime) -> datetime:
    """北京时间的今天零点（日上限按自然日算）。"""
    local = now.astimezone(BEIJING)
    return local.replace(hour=0, minute=0, second=0, microsecond=0)


def next_day(now: datetime) -> datetime:
    return day_start(now) + timedelta(days=1)


@dataclass(frozen=True)
class Usage:
    """一条渠道这会儿用了多少：近一分钟问了几次（渠道、全局）、今天取回几份。"""

    channel_minute: int
    global_minute: int
    fetched_today: int
    channel_oldest: datetime | None = None  # 近一分钟里渠道最早那次
    global_oldest: datetime | None = None


def wait_for(
    usage: Usage,
    *,
    rate_per_minute: int,
    daily_cap: int,
    global_rate: int,
    fetch: bool,
    now: datetime,
) -> datetime | None:
    """这次能不能问：能就回 None；不能就回最早什么时候能问。"""
    if fetch and usage.fetched_today >= daily_cap:
        return next_day(now)
    waits = []
    if usage.channel_minute >= rate_per_minute:
        waits.append((usage.channel_oldest or now) + timedelta(minutes=1))
    if usage.global_minute >= global_rate:
        waits.append((usage.global_oldest or now) + timedelta(minutes=1))
    if not waits:
        return None
    return max(max(waits), now + timedelta(seconds=1))


# —— 预计时间 ——


def estimate_seconds(
    channel: Channel,
    *,
    ahead: int,
    fetched_today: int,
    global_rate: int,
    now: datetime,
) -> int | None:
    """按队列与限速粗算还要多久（0045 第 3.3 节「预计时间按队列与限速算」）：排在前面的
    作业加上这一个，每个按问几次渠道、取回几份算；今天的份数不够就顺延到后面几天。
    渠道暂停、停用的算不出，回 None。"""
    if channel.status != "active":
        return None
    jobs = ahead + 1
    per_minute = max(1, min(channel.rate_per_minute, global_rate))
    seconds = math.ceil(jobs * REQUESTS_PER_JOB / per_minute * 60) + PROCESS_SECONDS
    need = jobs * FETCHES_PER_JOB
    left_today = max(0, channel.daily_cap - fetched_today)
    if need > left_today:
        days = math.ceil((need - left_today) / max(1, channel.daily_cap))
        until_tomorrow = (next_day(now) - now).total_seconds()
        seconds = int(until_tomorrow) + (days - 1) * 86400 + seconds
    return seconds
