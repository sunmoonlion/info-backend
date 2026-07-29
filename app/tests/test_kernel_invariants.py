from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_legacy_auth_shortcuts_are_absent() -> None:
    auth_service = (ROOT / "app/application/services/auth_service.py").read_text()
    auth_routes = (ROOT / "app/interfaces/endpoints/auth_routes.py").read_text()
    main = (ROOT / "app/main.py").read_text()

    assert "_decode_id_token_claims" not in auth_service
    assert "CREATE TABLE IF NOT EXISTS users" not in auth_service
    assert "json.dumps(tokens)" not in auth_service
    assert '@router.get("/logout"' not in auth_routes
    assert 'allow_origins=["*"]' not in main


def test_runtime_image_context_excludes_credentials_and_tests() -> None:
    dockerignore = (ROOT.parent / ".dockerignore").read_text().splitlines()
    assert "app/.env" in dockerignore
    assert "app/.env.*" in dockerignore
    assert "app/tests" in dockerignore
