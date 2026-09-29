from __future__ import annotations

from pydantic import BaseModel


class OriginRead(BaseModel):
    app: str
    ref: str | None = None
    # 回到原处的地址。只从这个应用自己的配置里来，从不从链接里来；没配就是空
    return_url: str | None = None


class TargetRead(BaseModel):
    web_base_url: str


class LinksRead(BaseModel):
    # 这个应用自己的名字：带人去别的应用时，链接里的 from 写它
    app: str
    targets: dict[str, TargetRead]
