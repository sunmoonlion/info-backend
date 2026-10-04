"""数据库会话只有一种做法：`make_session_factory`（`infrastructure/storage/postgres.py`）。

2026-10-04 真链路上发现的：生产的会话「提交后对象过期」，测试的会话不过期，两边不一样。
Worker 提交之后再读已加载对象的属性，在生产里报 `MissingGreenlet`，测试却一直是绿的。
现在生产与测试取同一个工厂，这里守着它不再分叉。
这个文件在模板与各应用里逐字相同。
"""

from __future__ import annotations

import re
import uuid
from pathlib import Path

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from test_durable_delivery_db import db as db

from app.infrastructure.models.auth import AuthUser
from app.infrastructure.storage.postgres import make_session_factory

ROOT = Path(__file__).resolve().parents[1]
THE_ONE_PLACE = "app/infrastructure/storage/postgres.py"


def test_the_options_every_session_gets():
    engine = create_async_engine("postgresql+asyncpg://nobody@127.0.0.1:1/none")
    options = make_session_factory(engine).kw
    assert options["expire_on_commit"] is False
    assert options["autoflush"] is False
    assert options["autocommit"] is False
    assert options["bind"] is engine


def test_nobody_builds_sessions_another_way():
    """生产代码和测试都不自己建会话工厂：自己建的那一份迟早和生产不一样。"""
    builds = re.compile(r"\b(async_)?sessionmaker\(")
    found = []
    for folder in ("app", "core", "tests", "alembic"):
        for path in sorted((ROOT / folder).rglob("*.py")):
            name = path.relative_to(ROOT).as_posix()
            if name in (THE_ONE_PLACE, "tests/test_session_config.py"):
                continue
            if builds.search(path.read_text()):
                found.append(name)
    assert found == []


def user(subject: str) -> AuthUser:
    return AuthUser(
        id=uuid.uuid4(), issuer="https://identity.example.test", subject=subject
    )


async def test_a_loaded_object_can_be_read_after_commit(db):  # noqa: F811
    """提交之后读属性不查库。提交后过期的会话在这里报 `MissingGreenlet`。"""
    async with db() as session:
        someone = user("read-after-commit")
        session.add(someone)
        await session.commit()
        assert someone.subject == "read-after-commit"
        someone.display_name = "第二次"
        await session.commit()
        assert (someone.subject, someone.display_name) == (
            "read-after-commit",
            "第二次",
        )


async def test_values_written_elsewhere_need_an_explicit_refresh(db):  # noqa: F811
    """代价：别的事务写进去的值不会自己出现。要新值，显式 `refresh` 或重新查询。"""
    async with db() as mine, db() as other:
        someone = user("refresh")
        mine.add(someone)
        await mine.commit()
        await other.execute(
            text("UPDATE auth_user SET display_name = '别人改的' WHERE id = :id"),
            {"id": someone.id},
        )
        await other.commit()
        assert someone.display_name is None
        await mine.refresh(someone)
        assert someone.display_name == "别人改的"
