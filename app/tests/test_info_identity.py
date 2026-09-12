import json

import pytest

from app.domain.info_identity_v1 import (
    IdentityURLInvalid,
    audit_identity_rows,
    identity_key_v1,
    normalize_url_v1,
)


@pytest.mark.parametrize(
    "url,expected",
    [
        ("HTTP://EXAMPLE.com:80#part", "http://example.com/"),
        ("https://Example.com:443?a=1&b=2#x", "https://example.com/?a=1&b=2"),
        ("https://例子.com/a", "https://xn--fsqu00a.com/a"),
        (
            "https://[2001:4860:4860:0:0:0:0:8888]:443",
            "https://[2001:4860:4860::8888]/",
        ),
        ("https://e.test?", "https://e.test/?"),
        ("https://e.test./x", "https://e.test./x"),
        ("https://e.test:444/x", "https://e.test:444/x"),
        ("upload://Report #1.md", "upload://Report #1.md"),
    ],
)
def test_normalization_policy(url, expected):
    assert normalize_url_v1(url) == expected
    assert identity_key_v1(url) == identity_key_v1(expected)


@pytest.mark.parametrize(
    "left,right",
    [
        ("http://e.test/a", "https://e.test/a"),
        ("https://e.test/a?x=1&x=2", "https://e.test/a?x=2&x=1"),
        ("https://e.test/a?utm_source=x", "https://e.test/a"),
        ("https://e.test/%2F", "https://e.test//"),
        ("https://e.test/A", "https://e.test/a"),
        ("https://e.test/a", "https://e.test/a/"),
        ("https://e.test/", "https://e.test/?"),
        ("upload://A.md", "upload://a.md"),
    ],
)
def test_significant_differences_preserved(left, right):
    assert identity_key_v1(left) != identity_key_v1(right)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "ftp://e.test",
        "https://user:secret@e.test",
        "https://e.test:bad",
        "https://e.test/\n",
        "https://e.test/\\a",
        "upload://",
        "upload://a\x00b",
    ],
)
def test_invalid_identity_has_no_sensitive_error(url):
    with pytest.raises(IdentityURLInvalid, match="^identity_url_invalid$"):
        identity_key_v1(url)


def test_audit_reports_collisions_without_urls_or_mutations():
    report = audit_identity_rows(
        [
            ("a", "https://E.test:443/a?token=secret#fragment"),
            ("b", "https://e.test/a?token=secret"),
            ("c", None),
            ("d", "not-a-url"),
        ]
    )
    assert report["ready"] is False
    assert report["complete"] is True
    assert report["duplicate_document_ids_sample"] == [["a", "b"]]
    assert report["null_urls"] == 1 and report["invalid_urls"] == 1
    assert "secret" not in json.dumps(report)
    assert "https" not in json.dumps(report)


def test_audit_capacity_is_not_silent_success(monkeypatch):
    from app.domain import info_identity_v1

    monkeypatch.setattr(info_identity_v1, "MAX_PREFLIGHT_ROWS", 1)
    report = audit_identity_rows([("a", None), ("b", None)])
    assert report["ready"] is False and report["complete"] is False


def test_null_identity_remains_null():
    assert identity_key_v1(None) is None
