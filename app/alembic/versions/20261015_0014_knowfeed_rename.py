"""改名 info → knowfeed 的库内部分（k8s/docs/rename.md R1，0025）

- 采集库四张带应用名前缀的表改名，连同名字里带它们的约束、索引、序列；
只改结构，不改数据：所有者 2026-10-15 定两个应用的现有数据都不要，新库从空的开始（库名、桶、角色都是新建的）。
迁移链从头建库时前面的迁移建出的是 info_* 表，由这一条改成新名。

Revision ID: 20261015_0014
Revises: 20260929_0013
"""

from __future__ import annotations

import re

import sqlalchemy as sa
from alembic import op

revision = "20261015_0014"
down_revision = "20260929_0013"
branch_labels = None
depends_on = None

TABLES = {
    "info_source": "knowfeed_source",
    "info_collector": "knowfeed_collector",
    "info_document": "knowfeed_document",
    "info_document_version": "knowfeed_document_version",
}
# 名字里的旧称 → 新称（先长后短）
NAME_MAP = {
    "up": (("info_document_version", "knowfeed_document_version"), ("info_document", "knowfeed_document"),
           ("info_collector", "knowfeed_collector"), ("info_source", "knowfeed_source")),
}
NAME_MAP["down"] = tuple((b, a) for a, b in NAME_MAP["up"])
NAME = {d: re.compile("(^|_)(" + "|".join(re.escape(a) for a, _ in pairs) + ")(?=_|$)") for d, pairs in NAME_MAP.items()}


def _rename_objects(direction: str) -> None:
    """约束先改（主键、唯一约束背后的索引随之改名），再改剩下的索引与序列。只动当前模式（生产是 public；测试夹具在独立模式里跑全部迁移）里名字匹配的对象。"""
    pattern, pairs = NAME[direction], dict(NAME_MAP[direction])
    bind = op.get_bind()
    constraints = bind.execute(sa.text(
        "select c.conname, t.relname from pg_constraint c join pg_class t on t.oid = c.conrelid "
        "join pg_namespace n on n.oid = t.relnamespace where n.nspname = current_schema()"
    )).all()
    for name, table in constraints:
        if pattern.search(name):
            fixed = pattern.sub(lambda m: m[1] + pairs[m[2]], name)
            op.execute(f'ALTER TABLE "{table}" RENAME CONSTRAINT "{name}" TO "{fixed}"')
    for kind, word in (("i", "INDEX"), ("S", "SEQUENCE")):
        rows = bind.execute(sa.text(
            "select c.relname from pg_class c join pg_namespace n on n.oid = c.relnamespace "
            "where n.nspname = current_schema() and c.relkind::text = :k"
        ), {"k": kind}).scalars().all()
        for name in rows:
            if pattern.search(name):
                fixed = pattern.sub(lambda m: m[1] + pairs[m[2]], name)
                op.execute(f'ALTER {word} "{name}" RENAME TO "{fixed}"')


def upgrade() -> None:
    for old, new in TABLES.items():
        op.rename_table(old, new)
    _rename_objects("up")


def downgrade() -> None:
    _rename_objects("down")
    for old, new in TABLES.items():
        op.rename_table(new, old)
