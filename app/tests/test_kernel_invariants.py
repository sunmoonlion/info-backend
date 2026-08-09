from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_legacy_split_backend_shortcuts_are_absent() -> None:
    auth_service = (ROOT / "app/application/services/auth_service.py").read_text()
    api = (ROOT / "app/bootstrap/api.py").read_text()
    routes = (ROOT / "app/interfaces/http/routes.py").read_text()

    assert "AdminAuthService" not in auth_service
    assert "WebAuthService" not in auth_service
    assert "ADMIN_BACKEND_INTERNAL_URL" not in api
    assert "WEB_BACKEND_INTERNAL_URL" not in api
    assert 'allow_origins=["*"]' not in api
    assert "admin_auth_router" in routes and "web_auth_router" in routes


def test_interface_partition_and_dependency_direction_are_explicit() -> None:
    assert (ROOT / "app/interfaces/http/admin/auth.py").is_file()
    assert (ROOT / "app/interfaces/http/web/auth.py").is_file()
    assert (ROOT / "app/interfaces/http/web/interactions.py").is_file()
    assert (ROOT / "app/bootstrap/api.py").is_file()
    assert (ROOT / "app/bootstrap/worker.py").is_file()
    assert (ROOT / "app/bootstrap/scheduler.py").is_file()
    assert (ROOT / "app/bootstrap/migration.py").is_file()
    assert (ROOT / "app/infrastructure/repositories/outbox.py").is_file()
    assert (ROOT / "app/infrastructure/security/service_identity.py").is_file()

    application_sources = "\n".join(
        path.read_text()
        for path in (ROOT / "app/application").rglob("*.py")
    )
    assert "app.interfaces" not in application_sources


def test_one_linear_canonical_migration_chain() -> None:
    revisions = sorted(
        path
        for path in (ROOT / "alembic/versions").glob("*.py")
        if path.name != "__init__.py"
    )
    assert [path.name for path in revisions] == [
        "20260706_0001_info_spider_mvp.py",
        "20260707_0002_source_governance.py",
        "20260712_0003_auth_identity.py",
        "20260714_0004_delivery_outbox.py",
        "20260809_0005_outbox_primitives.py",
    ]
    contents = [path.read_text() for path in revisions]
    assert sum("down_revision = None" in content for content in contents) == 1
    assert 'down_revision = "20260706_0001"' in contents[1]
    assert 'down_revision = "20260707_0002"' in contents[2]
    assert 'down_revision = "20260712_0003"' in contents[3]
    assert 'down_revision = "20260714_0004"' in contents[4]


def test_business_and_shared_outboxes_remain_distinct() -> None:
    from app.infrastructure.models import Base

    assert {
        "delivery_outbox_message",
        "outbox_message",
        "inbox_message",
    } <= set(Base.metadata.tables)

    delivery = Base.metadata.tables["delivery_outbox_message"]
    shared = Base.metadata.tables["outbox_message"]
    assert "aggregate_id" in delivery.c
    assert "idempotency_key" in delivery.c
    assert "aggregate_key" in shared.c
    assert "deduplication_key" in shared.c


def test_runtime_image_context_excludes_credentials_and_tests() -> None:
    dockerignore = (ROOT.parent / ".dockerignore").read_text().splitlines()
    assert "app/.env" in dockerignore
    assert "app/.env.*" in dockerignore
    assert "app/tests" in dockerignore


def test_candidate_does_not_claim_the_formal_release() -> None:
    project = (ROOT / "pyproject.toml").read_text()
    assert 'version = "2.0.0.dev0"' in project
    api = (ROOT / "app/bootstrap/api.py").read_text()
    assert 'version="2.0.0"' not in api
