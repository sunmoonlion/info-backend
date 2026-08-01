from core.config import get_settings


def test_database_url_uses_asyncpg_and_drops_sslmode(monkeypatch) -> None:
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql://info:secret@postgresql:5432/info_admin?sslmode=disable&application_name=info",
    )
    get_settings.cache_clear()
    settings = get_settings()
    try:
        assert settings.database_url == (
            "postgresql+asyncpg://info:secret@postgresql:5432/"
            "info_admin?application_name=info"
        )
    finally:
        get_settings.cache_clear()


def test_migration_database_url_uses_same_normalization(monkeypatch) -> None:
    monkeypatch.setenv(
        "MIGRATION_DATABASE_URL",
        "postgresql://migrator:secret@postgresql:5432/info_admin?sslmode=require",
    )
    get_settings.cache_clear()
    settings = get_settings()
    try:
        assert settings.migration_database_url == (
            "postgresql+asyncpg://migrator:secret@postgresql:5432/info_admin"
        )
    finally:
        get_settings.cache_clear()
