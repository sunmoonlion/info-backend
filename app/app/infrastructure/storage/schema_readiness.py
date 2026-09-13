"""Read-only schema readiness against this Backend image's migration scripts."""

from functools import lru_cache
from pathlib import Path

from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

READINESS_TIMEOUT_SECONDS = 2.0
_MIGRATIONS_PATH = Path(__file__).resolve().parents[3] / "alembic"


class SchemaNotReady(RuntimeError):
    """The database revision cannot serve this immutable Backend version."""


@lru_cache(maxsize=1)
def expected_schema_revision() -> str:
    # ScriptDirectory reads revision metadata, never executes env.py/upgrade.
    # Only packaged metadata is cached; database readiness is never cached.
    scripts = ScriptDirectory(str(_MIGRATIONS_PATH))
    heads = scripts.get_heads()
    if len(heads) != 1 or len(scripts.get_bases()) != 1:
        raise SchemaNotReady("invalid_packaged_migration_chain")
    return heads[0]


async def verify_schema_revision(session: AsyncSession) -> None:
    expected = expected_schema_revision()
    revisions = (
        (await session.execute(text("SELECT version_num FROM alembic_version LIMIT 2")))
        .scalars()
        .all()
    )
    if revisions != [expected]:
        raise SchemaNotReady("schema_revision_mismatch")
