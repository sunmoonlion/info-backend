"""跨应用跳转：页面拼链接要的地址、链接带来的「从哪来」（app/domain/cross_app.py）。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.domain.cross_app import clean_origin, known_apps, return_url
from app.domain.security import Principal
from app.interfaces.http.middleware.auth import get_web_current_user
from app.interfaces.schemas.cross_app import LinksRead, OriginRead, TargetRead
from core.config import Settings, get_settings

router = APIRouter(prefix="/web/v1/cross-app", tags=["Cross-app links"])


def cross_app_settings() -> Settings:
    """测试用依赖覆盖换配置。"""
    return get_settings()


WebUser = Annotated[Principal, Depends(get_web_current_user)]
Config = Annotated[Settings, Depends(cross_app_settings)]


@router.get("/links", response_model=LinksRead)
async def links(_: WebUser, settings: Config) -> LinksRead:
    """这个应用会把用户带去哪些应用、各自网页端的地址。页面只在一处用它拼链接。"""
    return LinksRead(
        app=settings.app_slug,
        targets={
            name: TargetRead(web_base_url=target.web_base_url)
            for name, target in settings.cross_app_targets().items()
        },
    )


@router.get("/origin", response_model=OriginRead | None)
async def origin(
    _: WebUser,
    settings: Config,
    source: Annotated[str | None, Query(alias="from", max_length=64)] = None,
    ref: Annotated[str | None, Query(max_length=256)] = None,
) -> OriginRead | None:
    """链接带来的「从哪来」：认得就返回它和回跳地址，认不得返回空。"""
    sources = settings.cross_app_sources()
    found = clean_origin(source, ref, known_apps=known_apps(sources))
    if found is None:
        return None
    return OriginRead(
        app=found.app,
        ref=found.ref,
        return_url=return_url(sources.get(found.app), found),
    )
