"""休眠能力的声明与校验（REQ-009 的机制部分）。

**问题**：本仓有一批"代码在、没接线"的能力。它们最容易被误读成已可用——
看到 `Outbox` 仓库类就以为事件在投递，看到 `beat_schedule` 入口就以为有定时任务。
投影文档里列了清单，但**文档不会自己变假**：能力接上了、或判据锚点被改名了，
清单照旧写着，读的人照旧被误导。

**机制**：清单以可执行判据的形式声明在这里，每条两个方向都能失败——

  ``anchor_exists``   锚点还在吗。**为 False 说明判据自己失效了**（文件改名、
                      类改名），此时 ``still_dormant`` 会因为"找不到"而假性通过。
                      这是本项目反复踩过的坑：把"没找到"当成"不存在"。
  ``still_dormant``   还休眠着吗。为 False 说明**能力已接线**，声明过期——
                      要删掉本条，并同步更新 `repos/info-app.md`「已知未实现」。

**机制的边界**：它保证**已声明的条目不会变陈旧**，但**发现不了新出现的休眠能力**
——那需要判断"这段代码本该接线却没接"，不可机械判定。新增休眠能力时手工加一条。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _exists(rel: str) -> bool:
    return (ROOT / rel).exists()


@dataclass(frozen=True)
class Dormant:
    name: str
    kind: str  # deliberate=模板有意留白 / pending=欠账
    evidence: str
    anchor_exists: Callable[[], bool]
    still_dormant: Callable[[], bool]


def _mounted_paths(prefix: str) -> list[str]:
    """create_app() 实际挂出来的、以 prefix 开头的路由。

    不要用文本匹配代替：router 前缀往往写在被引入的模块里，
    对 routes.py grep 会漏掉，判据就成了永远通过的空转。
    """
    from fastapi.routing import APIRoute

    from app.bootstrap.api import create_app

    return [
        r.path
        for r in create_app().routes
        if isinstance(r, APIRoute) and r.path.startswith(prefix)
    ]


def _domain_dirs() -> list[Path]:
    return [ROOT / "app/domain" / d for d in ("models", "repositories", "services")]


# 只认**共享（模板）**的 Outbox 符号。实例仓可能有自己的领域 outbox
# （如 info 的 DeliveryOutboxMessage，它是在用的），不能一见 "outbox" 就算命中。
# 词边界让 \bOutboxMessage\b 不会匹配到 DeliveryOutboxMessage。
_SHARED_OUTBOX = re.compile(
    r"\b(OutboxPublisher|OutboxRepository|SqlOutboxRepository"
    r"|OutboxMessage|InboxMessage)\b"
)


def _shared_outbox_used_by_services() -> bool:
    return any(
        _SHARED_OUTBOX.search(p.read_text(encoding="utf-8"))
        for p in (ROOT / "app/application/services").rglob("*.py")
    )


DORMANT: tuple[Dormant, ...] = (
    Dormant(
        name="domain/{models,repositories,services} 仍是模板空壳",
        kind="pending",
        evidence=(
            "本仓继承自模板的这三个包只有空 __init__.py；实际领域代码在 "
            "infrastructure/models/ 与 application/services/。"
            "即分层目录名与代码实际归属不一致，读目录名会误判"
        ),
        anchor_exists=lambda: all(d.is_dir() for d in _domain_dirs()),
        still_dormant=lambda: all(
            [p.name for p in d.iterdir() if p.suffix == ".py"] == ["__init__.py"]
            for d in _domain_dirs()
        ),
    ),
    Dormant(
        name="Outbox/Inbox 消费链路",
        kind="deliberate",
        evidence="Port、DTO、ORM、SQL 仓库类齐备，application/services 零调用",
        anchor_exists=lambda: (
            _exists("app/infrastructure/repositories/outbox.py")
            and _exists("app/application/ports/outbox.py")
        ),
        still_dormant=lambda: not _shared_outbox_used_by_services(),
    ),
    Dormant(
        name="web-interaction 运行时",
        kind="deliberate",
        evidence="默认适配器是 UnavailableWebInteractionAdapter，生产必定 503",
        anchor_exists=lambda: (
            "class UnavailableWebInteractionAdapter"
            in _read("app/application/services/web_interaction.py")
        ),
        still_dormant=lambda: (
            "return UnavailableWebInteractionAdapter()"
            in _read("app/application/services/web_interaction.py")
        ),
    ),
    Dormant(
        name="/api/internal/v1 入站面",
        kind="deliberate",
        evidence="中间件在，但没有任何 router 挂到该前缀",
        anchor_exists=lambda: _exists("app/interfaces/http/routes.py"),
        # 必须查**真实路由表**：前缀写在被引入的 endpoints 模块里，
        # 对 routes.py 做文本匹配会漏掉，判据会假性通过。
        still_dormant=lambda: not _mounted_paths("/api/internal"),
    ),
    Dormant(
        name="Celery 周期任务",
        kind="deliberate",
        evidence="Scheduler 是四个运行角色之一，但全仓无 beat_schedule 定义",
        anchor_exists=lambda: _exists("app/bootstrap/scheduler.py"),
        still_dormant=lambda: (
            not any(
                "beat_schedule" in p.read_text(encoding="utf-8")
                for p in (ROOT / "app").rglob("*.py")
            )
        ),
    ),
    Dormant(
        name="Elasticsearch 索引",
        kind="deliberate",
        evidence="SEARCH_BACKEND 默认 disabled，索引任务直接 skip；ES 是可选后端",
        anchor_exists=lambda: "SEARCH_BACKEND" in _read("core/config.py"),
        still_dormant=lambda: (
            'search_backend: str = Field(default="disabled"' in _read("core/config.py")
        ),
    ),
    Dormant(
        name="多下游分发",
        kind="deliberate",
        evidence="分发目标硬编码 knowledge-app，其余 target_app 一律拒绝",
        anchor_exists=lambda: (
            "target_app" in _read("app/application/services/info_crawl_service.py")
        ),
        still_dormant=lambda: (
            'if record.target_app != "knowledge-app":'
            in _read("app/application/services/info_crawl_service.py")
        ),
    ),
    Dormant(
        name="内置爬虫引擎",
        kind="deliberate",
        evidence="不内嵌 Scrapy/Playwright，采集结果须由外部注入",
        anchor_exists=lambda: _exists("pyproject.toml"),
        still_dormant=lambda: (
            not any(
                dep in _read("pyproject.toml").lower()
                for dep in ("scrapy", "playwright")
            )
        ),
    ),
)


@pytest.mark.parametrize("item", DORMANT, ids=lambda i: i.name)
def test_dormant_capability_anchor_still_exists(item: Dormant) -> None:
    """锚点消失 → 判据空转。必须先修判据，否则下一条检查是假通过。"""
    assert item.anchor_exists(), (
        f"休眠声明「{item.name}」的锚点不见了——判据已失效，会假性通过。"
        f"先把判据改到新位置；确已删除该能力，则删掉本条声明。"
    )


@pytest.mark.parametrize("item", DORMANT, ids=lambda i: i.name)
def test_declared_dormant_capability_is_still_dormant(item: Dormant) -> None:
    """能力接线了 → 声明过期。删条目，并同步投影文档。"""
    assert item.still_dormant(), (
        f"休眠声明「{item.name}」已不成立——该能力看起来已接线。"
        f"请删掉本条声明，并同步更新 k8s:sunmoonai/docs/project-guide/"
        f"repos/info-app.md 的「已知未实现」一节。依据：{item.evidence}"
    )


def test_every_dormant_entry_declares_why() -> None:
    """kind 只有两种：deliberate（有意留白）与 pending（欠账）。

    这一栏决定读者要不要担心：deliberate 是设计，pending 是债。
    """
    for item in DORMANT:
        assert item.kind in {"deliberate", "pending"}, item.name
        assert item.evidence.strip(), item.name
