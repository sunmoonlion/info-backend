from datetime import UTC, datetime
from uuid import UUID

from app.interfaces.schemas.info import SourceCreate, SourceRead


def test_source_create_defaults_governance_fields() -> None:
    payload = SourceCreate(code="example", name="Example")

    assert payload.trust_level == "unknown"
    assert payload.copyright_status == "unknown"
    assert payload.license_url is None
    assert payload.terms_url is None


def test_source_read_exposes_governance_fields() -> None:
    source = SourceRead(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        code="official",
        name="Official Source",
        source_type="website",
        base_url="https://example.com",
        status="active",
        trust_level="official",
        copyright_status="licensed",
        license_url="https://example.com/license",
        terms_url="https://example.com/terms",
        description=None,
        created_at=datetime(2026, 7, 7, tzinfo=UTC),
        updated_at=datetime(2026, 7, 7, tzinfo=UTC),
    )

    assert source.trust_level == "official"
    assert source.copyright_status == "licensed"
    assert source.license_url == "https://example.com/license"
    assert source.terms_url == "https://example.com/terms"
