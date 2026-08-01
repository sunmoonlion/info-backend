from __future__ import annotations

import pytest

from core.config import Settings


def _production_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "_env_file": None,
        "env": "production",
        "casdoor_endpoint": "https://identity.example.test",
        "casdoor_client_id": "tpl-admin-client",
        "casdoor_client_secret": "test-only-secret",
        "casdoor_redirect_uri": "https://admin.example.test/api/auth/callback",
        "frontend_base_url": "https://admin.example.test",
        "frontend_allowed_origins": "https://admin.example.test",
        "allowed_hosts": "admin.example.test",
    }
    values.update(overrides)
    return Settings(**values)


def test_database_urls_use_asyncpg_and_drop_sslmode() -> None:
    settings = Settings(
        _env_file=None,
        database_url=(
            "postgresql://tpl:secret@postgresql:5432/tpl?"
            "sslmode=disable&application_name=tpl"
        ),
        migration_database_url=(
            "postgresql://migrator:secret@postgresql:5432/tpl?sslmode=require"
        ),
    )
    assert settings.database_url == (
        "postgresql+asyncpg://tpl:secret@postgresql:5432/tpl?application_name=tpl"
    )
    assert settings.migration_url == (
        "postgresql+asyncpg://migrator:secret@postgresql:5432/tpl"
    )


def test_production_browser_identity_is_fail_fast() -> None:
    settings = _production_settings()
    settings.require_browser_identity()
    assert settings.auth_cookie_secure is True
    assert settings.session_cookie_name == "sunmoonai_info_admin_sid"
    assert settings.transaction_cookie_name == "sunmoonai_info_admin_oidc_tx"
    assert settings.session_key_prefix == "info:auth:admin:session:"


def test_production_rejects_missing_or_insecure_identity() -> None:
    with pytest.raises(ValueError, match="missing"):
        _production_settings(casdoor_client_secret="").require_browser_identity()
    with pytest.raises(ValueError, match="HTTPS"):
        _production_settings(
            casdoor_endpoint="http://identity.example.test"
        ).require_browser_identity()
    with pytest.raises(ValueError, match="SESSION_COOKIE_SECURE"):
        _production_settings(session_cookie_secure=False)
    with pytest.raises(ValueError, match="ALLOWED_HOSTS"):
        _production_settings(allowed_hosts="*")


def test_credential_cors_and_weak_algorithms_are_rejected() -> None:
    with pytest.raises(ValueError, match="wildcard"):
        Settings(_env_file=None, frontend_allowed_origins="*")
    with pytest.raises(ValueError, match="asymmetric"):
        _ = Settings(
            _env_file=None,
            auth_allowed_algorithms="HS256",
        ).auth_allowed_algorithm_list


@pytest.mark.parametrize(
    "origin",
    [
        "https://user@admin.example.test",
        "https://admin.example.test/path",
        "https://admin.example.test?query=1",
        "ftp://admin.example.test",
    ],
)
def test_frontend_origins_are_origin_only(origin: str) -> None:
    with pytest.raises(ValueError, match="origin"):
        Settings(_env_file=None, frontend_allowed_origins=origin)


def test_redirect_and_frontend_must_share_origin() -> None:
    settings = _production_settings(
        casdoor_redirect_uri="https://api.example.test/api/auth/callback"
    )
    with pytest.raises(ValueError, match="FRONTEND_BASE_URL origin"):
        settings.require_browser_identity()


def test_provider_discovery_and_redirect_paths_are_exact() -> None:
    with pytest.raises(ValueError, match="standard discovery path"):
        _production_settings(
            casdoor_discovery_url=(
                "https://identity.example.test/sunmoonai-tpl-admin/"
                ".well-known/openid-configuration"
            )
        ).require_browser_identity()
    with pytest.raises(ValueError, match="/api/auth/callback"):
        _production_settings(
            casdoor_redirect_uri="https://admin.example.test/other-callback"
        ).require_browser_identity()
