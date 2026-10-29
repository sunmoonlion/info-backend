"""预切合的契约 `knowfeed-supply-prematch/v1`（SDD 0045 第四节；发版前补齐 F9、C4）。

knowfeed 是提供方。确认之前，应用（knowbench 的请专家清单、knowlab）问 knowfeed 这几项
「这类能取 / 取不到 / 网站待审」、候选渠道的许可档、按队列估的预计时间。**只查来源目录**
（第三轮评审 F 2.6）：毫秒级，不在用户确认之前替他去问渠道，也不看 knowbase 已有什么；
「渠道里有没有这一份」放到受理后。

应用把回来的结果连同可接受的条件，照存进 knowbase 确认的供给条件快照（`supply`）：
`{"conditions": {"min_license": ..., "channels": [...]}, "prematch": <本接口的回答>}`；
受理时 knowfeed 只核 `conditions`，候选渠道都不满足的回「当前供给条件不满足」。

入口 `POST /api/internal/v1/supply/prematch`，同需求接口的服务身份与 scope
（关系 `supply-demand`，scope `knowfeed:supply-demand`）。

模型是唯一真源，父仓 `contracts/supply-prematch/v1/` 下的 schema 由它生成：
在后端目录下 `python -m app.domain.supply.schema`。
"""

from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CONTRACT = "knowfeed-supply-prematch/v1"
PATH = "/api/internal/v1/supply/prematch"
RELATION = "supply-demand"

LicenseV1 = Literal["self", "via_expert", "sale"]
ReadingV1 = Literal["original", "text", "pages", "table"]


class PrematchKeyV1(BaseModel):
    """一条描述（SDD 0049），同需求单的描述键。"""

    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=64)
    doc_type: str = Field(min_length=1, max_length=32)
    period_start: date | None = None
    period_end: date | None = None
    edition: Literal["full", "summary", "english"] = "full"
    topics: list[str] = Field(default_factory=list, max_length=12)
    industry: str | None = Field(default=None, max_length=16)


class PrematchItemV1(BaseModel):
    """问的一项：一条描述，或一个网址（给网址的需求）。两样给且只给一样。"""

    model_config = ConfigDict(extra="forbid")

    ref: str = Field(
        min_length=1, max_length=64, description="调用方自己的编号，原样带回"
    )
    key: PrematchKeyV1 | None = None
    url: str | None = Field(default=None, max_length=2000)
    readings: list[ReadingV1] = Field(
        default_factory=lambda: ["original"], max_length=4
    )


class ConditionsV1(BaseModel):
    """可接受的来源限制与用途条件（例如要随公开专家用，就要 via_expert 及更宽的）。"""

    model_config = ConfigDict(extra="forbid")

    min_license: LicenseV1 | None = None
    channels: list[str] = Field(
        default_factory=list, max_length=20, description="只用这几条渠道；空是不限"
    )


class PrematchRequestV1(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[PrematchItemV1] = Field(min_length=1, max_length=50)
    conditions: ConditionsV1 = Field(default_factory=ConditionsV1)


class CandidateV1(BaseModel):
    """一条候选渠道（按切合时试的先后）。"""

    channel_id: str
    name: str
    license: LicenseV1
    status: Literal["active", "paused"]
    readings: list[ReadingV1] = Field(description="这条渠道能给的读法")
    eta_seconds: int | None = Field(
        description="按队列与限速粗算还要多久；渠道暂停时算不出，是 null"
    )


PrematchStatusV1 = Literal[
    "available", "unavailable", "site_pending", "conditions_unmet"
]


class PrematchAnswerV1(BaseModel):
    ref: str
    status: PrematchStatusV1 = Field(
        description=(
            "available 这类能取；unavailable 取不到；site_pending 网站待审（审过才取）；"
            "conditions_unmet 能取的渠道都不满足给的条件"
        )
    )
    reason: str | None = Field(
        default=None,
        description=(
            "取不到的原因编码：no_channel（这类数据还取不到）、subject_unsupported、"
            "site_rejected、site_unregistered（没登记的网站，提了就自动建待审）、site_pending"
        ),
    )
    license: LicenseV1 | None = Field(
        default=None, description="先试的那条渠道取来的数据属于哪一档"
    )
    eta_seconds: int | None = Field(default=None, description="先试的那条渠道的预计")
    readings_missing: list[ReadingV1] = Field(
        default_factory=list, description="要了、但先试的那条渠道给不了的读法"
    )
    candidates: list[CandidateV1] = Field(default_factory=list)


class PrematchResponseV1(BaseModel):
    contract: Literal["knowfeed-supply-prematch/v1"] = CONTRACT
    items: list[PrematchAnswerV1]
