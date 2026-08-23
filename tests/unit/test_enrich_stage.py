"""The enrichment stage, end to end and offline.

The stage's contract is narrow and its failure behaviour is the interesting part: it must
read a bounded set of pages, keep every candidate whatever it finds, and never let one bad
page or one bad company take down the run.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.unit.web_fixtures import NOW, distinct_page, fetcher, html_response, load_html
from vc_scout.models.candidate import Candidate, CandidateSet
from vc_scout.models.enums import EnrichmentStatus, FetchFailure, PageRole, SourceKind
from vc_scout.models.report import EnrichmentReport
from vc_scout.models.source import SourceReference
from vc_scout.stages.enrich import MAX_EXTRA_PAGES, run_enrich
from vc_scout.store import RunStore

HOME = "https://acme.example/"
LAUNCH = "https://acme.example/blog/launch-post"


def routes(**extra: Any) -> dict[str, Any]:
    """The homepage plus the three internal pages it links to, each with a distinct body."""
    base: dict[str, Any] = {
        HOME: html_response(load_html("homepage")),
        "https://acme.example/product": html_response(distinct_page("product")),
        "https://acme.example/pricing": html_response(load_html("pricing")),
        "https://acme.example/customers": html_response(distinct_page("customers")),
        LAUNCH: html_response(distinct_page("launch")),
    }
    base.update(extra)
    return base


def seed(
    store: RunStore, *, website: str | None = HOME, launch: str | None = None, count: int = 1
) -> CandidateSet:
    """Write a candidates.json for ``count`` companies."""
    candidates, sources = [], []
    for index in range(count):
        host = website if count == 1 else f"https://co-{index}.example/"
        ids = []
        hn = SourceReference.create(
            f"https://news.ycombinator.com/item?id=100{index}", kind=SourceKind.HN_STORY
        )
        sources.append(hn)
        ids.append(hn.source_id)
        if launch:
            page = SourceReference.create(launch, kind=SourceKind.COMPANY_PAGE)
            sources.append(page)
            ids.append(page.source_id)
        candidates.append(
            Candidate(
                company_id="acme-ops" if count == 1 else f"co-{index}",
                name=f"Acme {index}",
                source_ids=ids,
                website=host,
            )
        )
    bundle = CandidateSet(run_id=store.run_id, query="q", candidates=candidates, sources=sources)
    store.write_candidates(bundle)
    return bundle


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    store = RunStore("source-test", runs_root=tmp_path)
    store.ensure_root()
    return store


def enrich(store: RunStore, route_map: dict[str, Any] | None = None, **kw: Any) -> Any:
    routed = routes() if route_map is None else route_map
    return run_enrich(store=store, fetcher=fetcher(routed, **kw), now=NOW)


# -- happy path --------------------------------------------------------------


def test_the_homepage_and_three_internal_pages_are_fetched(store: RunStore) -> None:
    seed(store)
    outcome = enrich(store)
    bundle = store.read_pages("acme-ops")

    assert bundle.status is EnrichmentStatus.SUCCESS
    assert [page.role for page in bundle.pages] == [
        PageRole.HOMEPAGE,
        PageRole.PRODUCT,
        PageRole.PRICING,
        PageRole.CUSTOMERS,
    ]
    assert len(bundle.pages) == 1 + MAX_EXTRA_PAGES
    assert outcome.report.counts["success"] == 1


def test_never_more_than_three_pages_beyond_the_homepage(store: RunStore) -> None:
    seed(store)
    for allowance in (0, 1, 2, 3):
        outcome = run_enrich(
            store=store, fetcher=fetcher(routes()), now=NOW, max_extra_pages=allowance
        )
        assert len(store.read_pages("acme-ops").pages) == 1 + allowance
        assert outcome.report.limits["max_extra_pages"] == allowance


def test_every_required_field_is_recorded_for_each_page(store: RunStore) -> None:
    seed(store)
    enrich(store)
    page = store.read_pages("acme-ops").pages[0]

    assert page.company_id == "acme-ops"
    assert page.url == HOME
    assert page.final_url == HOME
    assert page.title == "Acme Ops — AI invoicing for contractors"
    assert page.headings == ["Stop chasing invoices", "How it works"]
    assert page.http_status == 200
    assert page.content_type == "text/html"
    assert page.fetched_at == NOW
    assert len(page.content_sha256) == 64
    assert "reconciles invoices" in page.text
    assert page.truncated is False


def test_a_redirect_records_both_the_requested_and_final_url(store: RunStore) -> None:
    seed(store)
    enrich(
        store,
        routes(
            **{
                HOME: httpx.Response(301, headers={"location": "/home"}),
                "https://acme.example/home": html_response(load_html("homepage")),
            }
        ),
    )
    page = store.read_pages("acme-ops").pages[0]
    assert page.url == HOME
    assert page.final_url == "https://acme.example/home"


# -- launch URL vs website ---------------------------------------------------


def test_the_launch_url_is_fetched_as_a_distinct_page(store: RunStore) -> None:
    seed(store, launch=LAUNCH)
    enrich(store)
    bundle = store.read_pages("acme-ops")

    assert PageRole.LAUNCH in [page.role for page in bundle.pages]
    launch_page = next(p for p in bundle.pages if p.role is PageRole.LAUNCH)
    assert launch_page.url == LAUNCH
    # It takes one of the three additional slots rather than adding a fourth.
    assert len(bundle.pages) == 1 + MAX_EXTRA_PAGES


def test_a_launch_url_equal_to_the_homepage_is_not_fetched_twice(store: RunStore) -> None:
    seed(store, launch=HOME)
    enrich(store)
    roles = [page.role for page in store.read_pages("acme-ops").pages]
    assert roles.count(PageRole.HOMEPAGE) == 1
    assert PageRole.LAUNCH not in roles


def test_the_launch_page_seeds_link_discovery_when_the_homepage_fails(store: RunStore) -> None:
    seed(store, launch=LAUNCH)
    enrich(
        store, routes(**{HOME: httpx.Response(500), LAUNCH: html_response(load_html("homepage"))})
    )
    bundle = store.read_pages("acme-ops")

    assert bundle.status is EnrichmentStatus.PARTIAL
    assert PageRole.LAUNCH in [page.role for page in bundle.pages]
    assert len(bundle.pages) > 1, "links were still discovered from the launch page"


# -- deduplication -----------------------------------------------------------


def test_pages_redirecting_to_the_same_final_url_are_stored_once(store: RunStore) -> None:
    seed(store, launch=LAUNCH)
    enrich(store, routes(**{LAUNCH: httpx.Response(301, headers={"location": "/"})}))
    bundle = store.read_pages("acme-ops")

    finals = [page.final_url for page in bundle.pages]
    assert len(finals) == len(set(finals))
    assert store.read_enrichment_report().candidates[0].pages_deduplicated >= 1


def test_pages_with_identical_content_hashes_are_stored_once(store: RunStore) -> None:
    """Three internal paths, one shared body: a mirrored or aliased page."""
    shared = html_response(distinct_page("shared"))
    seed(store)
    enrich(
        store,
        routes(
            **{
                "https://acme.example/product": shared,
                "https://acme.example/pricing": shared,
                "https://acme.example/customers": shared,
            }
        ),
    )
    bundle = store.read_pages("acme-ops")

    hashes = [page.content_sha256 for page in bundle.pages]
    assert len(hashes) == len(set(hashes))
    assert len(bundle.pages) == 2, "homepage plus one distinct body"
    assert store.read_enrichment_report().candidates[0].pages_deduplicated == 2


# -- failure isolation -------------------------------------------------------


@pytest.mark.parametrize(
    ("response", "category"),
    [
        (httpx.Response(404), FetchFailure.HTTP_ERROR),
        (httpx.Response(403), FetchFailure.BLOCKED),
        (
            httpx.Response(200, text="{}", headers={"content-type": "application/json"}),
            FetchFailure.NON_HTML,
        ),
        (html_response(load_html("thin")), FetchFailure.EXTRACTION_FAILED),
    ],
)
def test_a_failed_page_is_categorised_without_failing_the_candidate(
    store: RunStore, response: httpx.Response, category: FetchFailure
) -> None:
    seed(store)
    enrich(store, routes(**{"https://acme.example/product": response}))
    bundle = store.read_pages("acme-ops")

    assert bundle.pages, "the homepage still succeeded"
    assert bundle.status is EnrichmentStatus.PARTIAL
    assert category in [failure.category for failure in bundle.failures]


def test_a_candidate_whose_site_is_unreachable_is_kept_with_no_pages(store: RunStore) -> None:
    seed(store)
    enrich(store, {})
    bundle = store.read_pages("acme-ops")

    assert bundle.status is EnrichmentStatus.FAILED
    assert bundle.pages == []
    assert bundle.failures
    assert bundle.warnings and "remains a candidate" in bundle.warnings[0]


def test_a_candidate_with_no_website_is_kept_and_recorded(store: RunStore) -> None:
    seed(store, website=None)
    enrich(store)
    bundle = store.read_pages("acme-ops")

    assert bundle.status is EnrichmentStatus.FAILED
    assert bundle.failures[0].category is FetchFailure.NO_WEBSITE


def test_one_failing_candidate_does_not_fail_the_run(store: RunStore) -> None:
    seed(store, count=3)
    outcome = enrich(
        store,
        {
            "https://co-0.example/": html_response(load_html("simple")),
            "https://co-2.example/": html_response(load_html("simple")),
        },
    )
    statuses = {row.company_id: row.status for row in outcome.report.candidates}

    assert len(statuses) == 3
    assert statuses["co-1"] is EnrichmentStatus.FAILED
    assert statuses["co-0"] is not EnrichmentStatus.FAILED
    # Every candidate keeps a bundle on disk, including the one that failed.
    assert store.extracted_company_ids() == ["co-0", "co-1", "co-2"]


def test_an_unexpected_client_fault_is_contained_to_one_candidate(store: RunStore) -> None:
    seed(store, count=2)

    def handler(request: httpx.Request) -> httpx.Response:
        if "co-0" in str(request.url):
            raise ValueError("something the client never anticipated")
        return html_response(load_html("simple"))

    outcome = run_enrich(
        store=store, fetcher=fetcher({}, transport=httpx.MockTransport(handler)), now=NOW
    )
    statuses = {row.company_id: row.status for row in outcome.report.candidates}
    assert statuses["co-0"] is EnrichmentStatus.FAILED
    assert statuses["co-1"] is not EnrichmentStatus.FAILED


def test_partial_success_is_distinguished_from_success_and_failure(store: RunStore) -> None:
    seed(store, count=3)
    outcome = enrich(
        store,
        {
            # co-0: a single self-contained page, nothing to follow, nothing to fail.
            "https://co-0.example/": html_response(load_html("simple")),
            # co-1: a homepage with links, one of which is broken.
            "https://co-1.example/": html_response(
                load_html("homepage").replace("https://acme.example", "https://co-1.example")
            ),
            "https://co-1.example/product": html_response(distinct_page("product")),
            "https://co-1.example/pricing": httpx.Response(500),
            "https://co-1.example/customers": html_response(distinct_page("customers")),
            # co-2: unreachable entirely.
        },
    )
    counts = outcome.report.counts
    assert counts["success"] == 1 and counts["partial"] == 1 and counts["failed"] == 1


# -- persistence -------------------------------------------------------------


def test_raw_bodies_and_metadata_are_persisted_for_replay(store: RunStore) -> None:
    seed(store)
    enrich(store)
    raw = store.resolve("raw", "web", "acme-ops")

    assert list(raw.glob("*.html"))
    metas = list(raw.glob("*.meta.json"))
    assert metas
    meta = json.loads(metas[0].read_text())
    assert {
        "requested_url",
        "final_url",
        "http_status",
        "content_type",
        "content_sha256",
        "bytes_read",
        "fetched_at",
        "body_path",
    } <= set(meta)
    assert not Path(meta["body_path"]).is_absolute()


def test_persisted_metadata_never_contains_headers_or_secrets(store: RunStore) -> None:
    seed(store)
    enrich(store)
    forbidden = (
        "authorization",
        "cookie",
        "set-cookie",
        "user-agent",
        "api_key",
        "api-key",
        "x-api-key",
        "ANTHROPIC",
        "secret",
        "token",
        "headers",
    )

    for path in store.resolve("raw", "web").rglob("*.meta.json"):
        blob = path.read_text().lower()
        for term in forbidden:
            assert term.lower() not in blob, f"{term} leaked into {path.name}"
    report_blob = store.enrichment_report_path().read_text().lower()
    assert "authorization" not in report_blob and "cookie" not in report_blob
    assert str(store.root) not in store.enrichment_report_path().read_text()


def test_artifacts_round_trip_through_the_store(store: RunStore) -> None:
    seed(store)
    outcome = enrich(store)
    assert store.read_enrichment_report() == outcome.report
    assert isinstance(store.read_enrichment_report(), EnrichmentReport)
    assert store.read_pages("acme-ops") == outcome.bundles[0]


def test_the_report_totals_the_whole_stage(store: RunStore) -> None:
    seed(store, count=2)
    report = enrich(store, {"https://co-0.example/": html_response(load_html("simple"))}).report

    assert report.counts["candidates"] == 2
    assert report.counts["pages_extracted"] >= 1
    assert report.failures_by_category
    assert set(report.limits) >= {
        "max_extra_pages",
        "max_text_chars",
        "max_response_bytes",
        "max_redirects",
    }
    assert report.notes


def test_no_investment_judgment_is_recorded(store: RunStore) -> None:
    """Enrichment decides what to read, never what it is worth."""
    seed(store)
    enrich(store)
    blob = store.extracted_path("acme-ops").read_text().lower()
    for term in ("score", "recommendation", "relevance_class", "rubric", "confidence"):
        assert term not in blob


# -- link resolution after a redirect ----------------------------------------
#
# Regression cover for the defect found by the first live run: link discovery used the
# requested homepage URL as its base, so a homepage that redirected produced follow-up URLs
# the site had never published, and scoped the same-origin filter to the abandoned host.

MOVED = "https://new.example/"


def moved_homepage() -> str:
    """The committed homepage, re-hosted, plus a link back to the original host."""
    return (
        load_html("homepage")
        .replace("https://acme.example", MOVED.rstrip("/"))
        .replace(
            '<a href="/product">Product</a>',
            '<a href="/product">Product</a><a href="https://acme.example/pricing">Old pricing</a>',
        )
    )


def cross_host_routes(**extra: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        HOME: httpx.Response(301, headers={"location": MOVED}),
        MOVED: html_response(moved_homepage()),
        "https://new.example/product": html_response(distinct_page("product")),
        "https://new.example/pricing": html_response(distinct_page("pricing")),
        "https://new.example/customers": html_response(distinct_page("customers")),
        # The abandoned host must never be asked for a follow-up page. Serving 500 here
        # means any regression shows up as a failure rather than passing silently.
        "https://acme.example/product": httpx.Response(500),
        "https://acme.example/pricing": httpx.Response(500),
        "https://acme.example/customers": httpx.Response(500),
    }
    base.update(extra)
    return base


def test_relative_links_resolve_against_the_final_host_after_a_redirect(
    store: RunStore,
) -> None:
    seed(store)
    enrich(store, cross_host_routes())
    urls = [page.final_url for page in store.read_pages("acme-ops").pages]

    assert urls[0] == MOVED
    assert "https://new.example/product" in urls
    assert "https://new.example/pricing" in urls


def test_the_stale_host_is_never_requested_for_a_follow_up_page(store: RunStore) -> None:
    requested: list[httpx.Request] = []
    seed(store)
    run_enrich(store=store, fetcher=fetcher(cross_host_routes(), record=requested), now=NOW)

    followups = [
        str(request.url)
        for request in requested
        if not str(request.url).endswith(("/robots.txt", HOME))
    ]
    assert followups, "follow-up pages were fetched"
    assert not any(url.startswith("https://acme.example/") for url in followups)
    assert store.read_pages("acme-ops").status is EnrichmentStatus.SUCCESS


def test_same_origin_filtering_uses_the_final_host(store: RunStore) -> None:
    """A link back to the host the page redirected away from is now cross-origin."""
    seed(store)
    enrich(store, cross_host_routes())
    urls = [page.final_url for page in store.read_pages("acme-ops").pages]

    # Assert presence first: absence alone would also hold if nothing had been fetched.
    assert len(urls) == 1 + MAX_EXTRA_PAGES
    assert all(url.startswith(MOVED) for url in urls)
    # The page carries an explicit anchor to the abandoned host; it must not be followed.
    assert "https://acme.example/pricing" in moved_homepage()
    assert not any(url.startswith("https://acme.example/") for url in urls)


def test_the_requested_url_and_redirect_chain_stay_persisted(store: RunStore) -> None:
    """The fix changes where links resolve from, never what is recorded."""
    seed(store)
    enrich(store, cross_host_routes())

    homepage = store.read_pages("acme-ops").pages[0]
    assert homepage.url == HOME
    assert homepage.final_url == MOVED

    metas = [
        json.loads(path.read_text())
        for path in store.resolve("raw", "web", "acme-ops").glob("*.meta.json")
    ]
    seed_meta = next(meta for meta in metas if meta["role"] == "homepage")
    assert seed_meta["requested_url"] == HOME
    assert seed_meta["final_url"] == MOVED
    assert seed_meta["redirects"] == [HOME]


def test_a_same_host_redirect_to_another_base_path_rebases_relative_links(
    store: RunStore,
) -> None:
    """The agentic-commits case: same host, deeper base path, document-relative links."""
    landing = "https://acme.example/projects/northwind/"
    seed(store)
    enrich(
        store,
        {
            HOME: httpx.Response(302, headers={"location": landing}),
            landing: html_response(load_html("relative-links")),
            "https://acme.example/projects/northwind/pricing": html_response(
                distinct_page("pricing")
            ),
            "https://acme.example/projects/northwind/about": html_response(distinct_page("about")),
            # Resolution against the requested root would land here instead.
            "https://acme.example/pricing": httpx.Response(500),
            "https://acme.example/about": httpx.Response(500),
        },
    )
    urls = [page.final_url for page in store.read_pages("acme-ops").pages]

    assert urls[0] == landing
    assert "https://acme.example/projects/northwind/pricing" in urls
    assert "https://acme.example/projects/northwind/about" in urls
    assert store.read_pages("acme-ops").status is EnrichmentStatus.SUCCESS


def test_link_resolution_is_unchanged_when_nothing_redirects(store: RunStore) -> None:
    """The non-redirect path must behave exactly as before the fix."""
    seed(store)
    enrich(store)
    bundle = store.read_pages("acme-ops")

    assert [page.role for page in bundle.pages] == [
        PageRole.HOMEPAGE,
        PageRole.PRODUCT,
        PageRole.PRICING,
        PageRole.CUSTOMERS,
    ]
    assert all(page.url == page.final_url for page in bundle.pages)
    assert bundle.status is EnrichmentStatus.SUCCESS


def test_the_seed_page_is_not_reselected_as_its_own_follow_up(store: RunStore) -> None:
    """A redirect target with a path can be self-linked; it must not be fetched twice."""
    landing = "https://acme.example/projects/northwind/"
    html = load_html("relative-links").replace(
        '<a href="pricing">Pricing</a>',
        f'<a href="{landing}">Home</a><a href="pricing">Pricing</a>',
    )
    seed(store)
    enrich(
        store,
        {
            HOME: httpx.Response(302, headers={"location": landing}),
            landing: html_response(html),
            "https://acme.example/projects/northwind/pricing": html_response(
                distinct_page("pricing")
            ),
            "https://acme.example/projects/northwind/about": html_response(distinct_page("about")),
        },
    )
    bundle = store.read_pages("acme-ops")
    urls = [page.final_url for page in bundle.pages]

    # The follow-ups still had to succeed, so the count below is not vacuous.
    assert bundle.status is EnrichmentStatus.SUCCESS
    assert "https://acme.example/projects/northwind/pricing" in urls
    assert urls.count(landing) == 1
