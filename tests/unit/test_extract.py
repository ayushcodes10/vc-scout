"""HTML to text, and deterministic internal-link selection."""

from __future__ import annotations

import pytest

from tests.unit.web_fixtures import load_html
from vc_scout.extract import (
    MAX_TEXT_CHARS,
    extract_content,
    role_for_path,
    select_internal_links,
)
from vc_scout.models.enums import PageRole

HOME = "https://acme.example/"


def home() -> str:
    return load_html("homepage")


# -- extraction --------------------------------------------------------------


def test_the_page_title_is_extracted() -> None:
    assert extract_content(home()).title == "Acme Ops — AI invoicing for contractors"


def test_headings_are_extracted_in_document_order() -> None:
    assert extract_content(home()).headings == ["Stop chasing invoices", "How it works"]


def test_meaningful_paragraphs_and_list_items_survive() -> None:
    text = extract_content(home()).text
    assert "reconciles invoices for plumbing contractors" in text
    assert "Syncs with QuickBooks and Xero" in text
    assert "matches payments to jobs" in text


def test_the_meta_description_is_kept_as_the_first_line() -> None:
    text = extract_content(home()).text
    assert text.splitlines()[0] == "Acme Ops reconciles invoices for plumbing contractors."


def test_navigation_header_and_footer_boilerplate_is_removed() -> None:
    text = extract_content(home()).text
    assert "Copyright 2026" not in text
    assert "Log in" not in text
    assert "Privacy" not in text


def test_scripts_and_styles_never_reach_the_text() -> None:
    text = extract_content(home()).text
    assert "window.analytics" not in text
    assert "margin" not in text


def test_main_content_is_preferred_over_the_whole_body() -> None:
    html = """<html><body><div>chrome noise that should not win</div>
    <main><p>The company describes what it actually sells right here.</p></main></body></html>"""
    assert "chrome noise" not in extract_content(html).text


def test_article_is_used_when_there_is_no_main() -> None:
    assert "Simple pricing" in extract_content(load_html("pricing")).text


def test_whitespace_is_normalised() -> None:
    html = "<html><body><main><p>spread   over\n\n  several     lines</p></main></body></html>"
    assert "spread over several lines" in extract_content(html).text


def test_repeated_blocks_are_emitted_once() -> None:
    html = """<html><body><main><p>Repeated boilerplate line.</p>
    <p>Repeated boilerplate line.</p><p>Something else entirely here.</p></main></body></html>"""
    assert extract_content(html).text.count("Repeated boilerplate line.") == 1


def test_text_is_bounded_and_truncation_is_recorded() -> None:
    # Each paragraph is distinct, so deduplication does not shrink the document first.
    body = "".join(f"<p>sentence number {i} of filler text</p>" for i in range(3000))
    html = f"<html><body><main>{body}</main></body></html>"
    content = extract_content(html)
    assert content.truncated is True
    assert len(content.text) <= MAX_TEXT_CHARS


def test_short_content_is_not_marked_truncated() -> None:
    assert extract_content(home()).truncated is False


def test_an_empty_document_yields_empty_content_rather_than_raising() -> None:
    content = extract_content("")
    assert content.text == ""
    assert content.title is None


def test_malformed_html_is_survivable() -> None:
    content = extract_content("<html><body><main><p>unclosed <b>tags")
    assert "unclosed" in content.text


# -- link selection ----------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "role"),
    [
        ("/pricing", PageRole.PRICING),
        ("/plans/annual", PageRole.PRICING),
        ("/about-us", PageRole.ABOUT),
        ("/team", PageRole.TEAM),
        ("/customers/acme", PageRole.CUSTOMERS),
        ("/case-studies/x", PageRole.CUSTOMERS),
        ("/features", PageRole.PRODUCT),
        ("/changelog", PageRole.CHANGELOG),
        ("/blog/hello", PageRole.BLOG),
        ("/nothing-relevant", None),
    ],
)
def test_role_for_path(path: str, role: PageRole | None) -> None:
    assert role_for_path(path) is role


def test_relevant_links_are_selected_in_priority_order() -> None:
    links = select_internal_links(home(), base_url=HOME, limit=3)
    assert [link.role for link in links] == [PageRole.PRODUCT, PageRole.PRICING, PageRole.CUSTOMERS]
    assert [link.url for link in links] == [
        "https://acme.example/product",
        "https://acme.example/pricing",
        "https://acme.example/customers",
    ]


@pytest.mark.parametrize("limit", [0, 1, 2, 3])
def test_the_page_limit_is_respected(limit: int) -> None:
    """A limit of zero must select nothing, not everything."""
    assert len(select_internal_links(home(), base_url=HOME, limit=limit)) == limit


def test_only_same_origin_links_are_followed() -> None:
    urls = [link.url for link in select_internal_links(home(), base_url=HOME, limit=3)]
    assert all(url.startswith("https://acme.example/") for url in urls)
    assert not any("twitter.com" in url or "other.example" in url for url in urls)


def test_a_protocol_relative_link_to_another_host_is_not_same_origin() -> None:
    html = '<html><body><a href="//evil.example/pricing">Pricing</a></body></html>'
    assert select_internal_links(html, base_url=HOME, limit=3) == []


def test_login_walls_legal_pages_and_assets_are_never_selected() -> None:
    html = """<html><body>
      <a href="/login">Login</a><a href="/privacy">Privacy</a><a href="/terms">Terms</a>
      <a href="/careers">Careers</a><a href="/pricing.pdf">Pricing PDF</a>
      <a href="/feed">RSS</a><a href="/pricing">Pricing</a>
    </body></html>"""
    links = select_internal_links(html, base_url=HOME, limit=3)
    assert [link.url for link in links] == ["https://acme.example/pricing"]


def test_at_most_one_page_per_role_is_selected() -> None:
    html = """<html><body>
      <a href="/blog/one">One</a><a href="/blog/two">Two</a><a href="/blog/three">Three</a>
    </body></html>"""
    assert len(select_internal_links(html, base_url=HOME, limit=3)) == 1


def test_the_shallowest_path_wins_within_a_role() -> None:
    html = """<html><body>
      <a href="/company/about/history/detail">Deep</a><a href="/about">Shallow</a>
    </body></html>"""
    assert (
        select_internal_links(html, base_url=HOME, limit=1)[0].url == "https://acme.example/about"
    )


def test_selection_is_deterministic() -> None:
    first = select_internal_links(home(), base_url=HOME, limit=3)
    second = select_internal_links(home(), base_url=HOME, limit=3)
    assert [link.url for link in first] == [link.url for link in second]


def test_excluded_urls_are_not_reselected() -> None:
    links = select_internal_links(
        home(), base_url=HOME, limit=3, exclude={"https://acme.example/product"}
    )
    assert "https://acme.example/product" not in [link.url for link in links]


def test_fragments_and_query_strings_are_canonicalised() -> None:
    html = '<html><body><a href="/pricing?utm_source=hn#plans">Pricing</a></body></html>'
    assert (
        select_internal_links(html, base_url=HOME, limit=1)[0].url == "https://acme.example/pricing"
    )


def test_the_homepage_is_never_selected_as_a_follow_up() -> None:
    html = '<html><body><a href="/">Home</a><a href="/pricing">Pricing</a></body></html>'
    urls = [link.url for link in select_internal_links(html, base_url=HOME, limit=3)]
    assert HOME not in urls
