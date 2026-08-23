"""Identifier stability.

These IDs are the spine of the citation chain. If they are not stable, citations cannot
be compared between runs.
"""

from __future__ import annotations

import pytest

from vc_scout.util.ids import (
    company_id_for,
    evidence_id_for,
    is_valid_company_id,
    is_valid_run_id,
    normalize_url,
    registrable_domain,
    slugify,
    source_id_for,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("HTTPS://Example.COM/About/", "https://example.com/About"),
        ("https://example.com:443/pricing", "https://example.com/pricing"),
        ("https://example.com/p?utm_source=hn&b=2&a=1", "https://example.com/p?a=1&b=2"),
        ("https://example.com/#team", "https://example.com/"),
        ("https://example.com", "https://example.com/"),
        # The host is never rewritten: www.example.com is a different URL.
        ("https://WWW.example.com/a", "https://www.example.com/a"),
    ],
)
def test_normalize_url_canonicalises(raw: str, expected: str) -> None:
    assert normalize_url(raw) == expected


def test_source_id_is_stable_across_cosmetic_url_differences() -> None:
    a = source_id_for("https://example.com/about")
    b = source_id_for("HTTPS://Example.com/about/?utm_campaign=x")
    assert a == b
    assert a.startswith("src-")


def test_source_id_differs_for_different_pages() -> None:
    assert source_id_for("https://example.com/a") != source_id_for("https://example.com/b")


def test_evidence_id_ignores_citation_order_and_whitespace() -> None:
    a = evidence_id_for("acme", "Charges  per seat", ["src-000000000001", "src-000000000002"])
    b = evidence_id_for("acme", "Charges per seat", ["src-000000000002", "src-000000000001"])
    assert a == b
    assert a.startswith("ev-")


def test_evidence_id_is_scoped_to_company() -> None:
    claim, sources = "Charges per seat", ["src-000000000001"]
    assert evidence_id_for("acme", claim, sources) != evidence_id_for("other", claim, sources)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Acme Ops, Inc.", "acme-ops-inc"), ("  Café Flow  ", "cafe-flow"), ("!!!", "")],
)
def test_slugify(raw: str, expected: str) -> None:
    assert slugify(raw) == expected


def test_company_id_falls_back_to_domain_then_digest() -> None:
    assert company_id_for("Acme Ops") == "acme-ops"
    # An unusable name falls back to the website's domain, dots included.
    assert company_id_for("!!!", "https://acme-ops.com") == "acme-ops-com"
    unnamed = company_id_for("!!!", None)
    assert unnamed.startswith("c-")
    assert is_valid_company_id(unnamed)


def test_registrable_domain_strips_www() -> None:
    assert registrable_domain("https://www.example.com/x") == "example.com"


@pytest.mark.parametrize("bad", ["../escape", "Has Space", "-leading", "UPPER", ""])
def test_invalid_company_ids_are_rejected(bad: str) -> None:
    assert not is_valid_company_id(bad)


@pytest.mark.parametrize("good", ["demo", "ai-agents-smb-demo", "run.2026-08-23"])
def test_valid_run_ids(good: str) -> None:
    assert is_valid_run_id(good)


@pytest.mark.parametrize("bad", ["../etc", "Run Id", "/abs", ""])
def test_invalid_run_ids_are_rejected(bad: str) -> None:
    assert not is_valid_run_id(bad)
