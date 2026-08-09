import json
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
LEGACY_INFRASTRUCTURE_NAMES = (
    "info-admin-backend",
    "info-web-backend",
    "info_admin_backend",
    "info_admin_user",
)


def test_access_declarations_use_unified_info_backend_identity() -> None:
    for relative_path in (
        "search-access-bootstrap/config/access.json",
        "storage-access-bootstrap/config/access.json",
    ):
        declaration = json.loads((BACKEND_ROOT / relative_path).read_text())
        assert declaration["metadata"]["name"] == "info-backend"
        assert declaration["spec"]["app"] == "info-app"
        assert declaration["spec"]["backend"] == "info-backend"


def test_database_bootstrap_has_no_legacy_identity_or_committed_password() -> None:
    for config_path in sorted(
        (BACKEND_ROOT / "db-access-bootstrap" / "config").glob("*.env")
    ):
        content = config_path.read_text()
        assert not any(name in content for name in LEGACY_INFRASTRUCTURE_NAMES)

        for line in content.splitlines():
            if "_PASSWORD=" not in line:
                continue
            _, value = line.split("=", maxsplit=1)
            assert value in {"", "change_me_via_secret_manager"}
