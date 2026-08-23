"""Stage 2 - enrich.

Reads ``candidates.json`` and, for each candidate, fetches a bounded set of pages from the
company's own website: the homepage, the exact URL posted to Hacker News when it differs,
and up to three deterministically chosen internal pages. What comes back is reduced to
readable text and persisted.

Three rules shape the whole stage:

* **No candidate is ever removed.** A company whose site is unreachable gets an empty page
  bundle with recorded failures, not deletion. Missing data has to stay visible to the
  stages that judge the company, otherwise absence of evidence silently becomes absence of
  the company.
* **No quality judgment happens here.** Enrichment decides what to read, never what it is
  worth. A one-page site and a fifty-page site are both simply read.
* **Nothing is fatal.** A failed page does not fail its candidate; a failed candidate does
  not fail the run.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from vc_scout.extract import MAX_TEXT_CHARS, extract_content, select_internal_links
from vc_scout.models.candidate import Candidate, CandidateSet
from vc_scout.models.enums import EnrichmentStatus, FetchFailure, PageRole, SourceKind
from vc_scout.models.page import ExtractedPage, PageBundle, PageFailure
from vc_scout.models.report import CandidateEnrichment, EnrichmentReport
from vc_scout.models.source import SourceReference
from vc_scout.net.http import FetchedPage, FetchError, SafeFetcher
from vc_scout.store import RunStore
from vc_scout.util.ids import normalize_url
from vc_scout.util.jsonio import write_json

__all__ = ["MAX_EXTRA_PAGES", "EnrichOutcome", "run_enrich"]

#: Additional pages beyond the homepage. The brief's ceiling, and a deliberate one: a
#: crawler that reads a whole site is a different tool with different obligations.
MAX_EXTRA_PAGES = 3

#: A page with less text than this carries no usable evidence; it is recorded as an
#: extraction failure so the gap is visible rather than looking like a successful read.
MIN_USEFUL_CHARS = 80


@dataclass(slots=True)
class EnrichOutcome:
    """What the stage produced, for the CLI to summarise."""

    report: EnrichmentReport
    bundles: list[PageBundle]
    report_path: str


@dataclass(slots=True)
class _CandidateRun:
    """Mutable working state for one candidate, sealed into a bundle and a report row."""

    candidate: Candidate
    pages: list[ExtractedPage] = field(default_factory=list)
    sources: dict[str, SourceReference] = field(default_factory=dict)
    failures: list[PageFailure] = field(default_factory=list)
    attempted: int = 0
    deduplicated: int = 0
    seen_urls: set[str] = field(default_factory=set)
    seen_hashes: set[str] = field(default_factory=set)

    @property
    def status(self) -> EnrichmentStatus:
        if not self.pages:
            return EnrichmentStatus.FAILED
        return EnrichmentStatus.PARTIAL if self.failures else EnrichmentStatus.SUCCESS

    def fail(
        self,
        url: str,
        category: FetchFailure,
        detail: str,
        *,
        role: PageRole | None = None,
        status: int | None = None,
    ) -> None:
        self.failures.append(
            PageFailure(url=url, category=category, role=role, detail=detail, http_status=status)
        )


def _launch_url(candidate: Candidate, sources: dict[str, SourceReference]) -> str | None:
    """The exact URL posted to Hacker News, recorded by the source stage.

    Kept distinct from ``candidate.website``: the website is the site origin used for
    enrichment, while this is the page the launch actually pointed at.
    """
    for source_id in candidate.source_ids:
        reference = sources.get(source_id)
        if reference is not None and reference.kind is SourceKind.COMPANY_PAGE:
            return reference.url
    return None


def _store_raw(store: RunStore, company_id: str, page: FetchedPage, role: PageRole) -> str:
    """Persist the page body and its fetch metadata for replay.

    The metadata deliberately contains no request headers, cookies or credentials - none
    are sent, and none are recorded. Only the response facts needed to replay and audit the
    fetch are kept.
    """
    body_path = store.raw_web_path(company_id, page.requested_url)
    store.write_text(body_path, page.body)
    meta_path = store.raw_web_path(company_id, page.requested_url, suffix=".meta.json")
    write_json(
        meta_path,
        {
            "company_id": company_id,
            "role": role.value,
            "requested_url": page.requested_url,
            "final_url": page.final_url,
            "redirects": list(page.redirects),
            "http_status": page.status,
            "content_type": page.content_type,
            "content_sha256": page.sha256,
            "bytes_read": page.bytes_read,
            "body_truncated": page.body_truncated,
            "fetched_at": page.fetched_at.isoformat(),
            "body_path": store.relative(body_path),
        },
    )
    return store.relative(body_path)


def _fetch_and_extract(
    run: _CandidateRun,
    fetcher: SafeFetcher,
    store: RunStore,
    url: str,
    role: PageRole,
) -> tuple[str, str] | None:
    """Fetch one page, extract it and record it.

    Returns ``(final_url, html)`` when the page was retrieved, or ``None`` when it was not.
    The final URL is returned rather than only the HTML because relative links in the body
    are relative to *where the response came from*, not to what was requested. A homepage
    that redirects to another host or another base path would otherwise have its links
    resolved against a URL the document never lived at.

    Every failure mode is caught and categorised here, so one unreadable page can never
    take down the candidate it belongs to.
    """
    company_id = run.candidate.company_id
    canonical = normalize_url(url)
    if canonical in run.seen_urls:
        run.deduplicated += 1
        return None

    run.attempted += 1
    run.seen_urls.add(canonical)

    try:
        fetched = fetcher.fetch_html(url)
    except FetchError as exc:
        run.fail(url, exc.category, exc.detail, role=role, status=exc.status)
        return None
    except Exception as exc:  # noqa: BLE001 - an unexpected client fault is still just a
        # failed page. Losing a candidate to an unanticipated exception would be worse.
        run.fail(url, FetchFailure.CONNECTION_ERROR, f"unexpected {type(exc).__name__}", role=role)
        return None

    # A redirect can land two requested URLs on the same page.
    final_canonical = normalize_url(fetched.final_url)
    if final_canonical != canonical and final_canonical in run.seen_urls:
        run.deduplicated += 1
        return fetched.final_url, fetched.body
    run.seen_urls.add(final_canonical)

    if fetched.sha256 in run.seen_hashes:
        # Identical bytes under a different URL: a mirrored or aliased page.
        run.deduplicated += 1
        return fetched.final_url, fetched.body
    run.seen_hashes.add(fetched.sha256)

    content = extract_content(fetched.body, max_chars=MAX_TEXT_CHARS)
    if len(content.text) < MIN_USEFUL_CHARS:
        run.fail(
            url,
            FetchFailure.EXTRACTION_FAILED,
            f"only {len(content.text)} characters of readable text were recovered",
            role=role,
            status=fetched.status,
        )
        return fetched.final_url, fetched.body

    _store_raw(store, company_id, fetched, role)
    reference = SourceReference.create(
        fetched.final_url,
        kind=SourceKind.COMPANY_PAGE,
        title=content.title,
        retrieved_at=fetched.fetched_at,
    )
    run.sources.setdefault(reference.source_id, reference)
    run.pages.append(
        ExtractedPage(
            company_id=company_id,
            source_id=reference.source_id,
            url=canonical,
            final_url=fetched.final_url,
            text=content.text,
            content_sha256=fetched.sha256,
            role=role,
            title=content.title,
            headings=content.headings,
            http_status=fetched.status,
            content_type=fetched.content_type,
            fetched_at=fetched.fetched_at,
            extractor=content.extractor,
            truncated=content.truncated,
            body_truncated=fetched.body_truncated,
        )
    )
    return fetched.final_url, fetched.body


def _enrich_candidate(
    candidate: Candidate,
    *,
    sources: dict[str, SourceReference],
    fetcher: SafeFetcher,
    store: RunStore,
    max_extra: int,
) -> _CandidateRun:
    """Fetch the homepage, then a bounded set of follow-up pages."""
    run = _CandidateRun(candidate=candidate)

    if not candidate.website:
        run.fail("", FetchFailure.NO_WEBSITE, "the candidate has no website recorded")
        return run

    homepage = candidate.website
    seed = _fetch_and_extract(run, fetcher, store, homepage, PageRole.HOMEPAGE)

    launch = _launch_url(candidate, sources)
    queue: list[tuple[str, PageRole]] = []
    if launch and normalize_url(launch) != normalize_url(homepage):
        # The launch page is the strongest single signal on the site, so it takes the first
        # of the additional slots.
        queue.append((launch, PageRole.LAUNCH))

    if seed is None and launch:
        # The homepage was unreadable. Fall back to the launch page as the link seed.
        seed = _fetch_and_extract(run, fetcher, store, launch, PageRole.LAUNCH)
        queue = []

    if seed is not None:
        seed_url, seed_html = seed
        # Link discovery is anchored to the URL the seed page was actually served from.
        # After a redirect that changes host or base path, resolving against the requested
        # URL produces addresses the site never published, and scopes the same-origin
        # filter to a host the document no longer belongs to.
        links = select_internal_links(
            seed_html,
            base_url=seed_url,
            limit=max(max_extra - len(queue), 0),
            exclude={homepage, seed_url, *([launch] if launch else [])},
        )
        queue.extend((link.url, link.role) for link in links)

    for url, role in queue[:max_extra]:
        _fetch_and_extract(run, fetcher, store, url, role)
    return run


def run_enrich(
    *,
    store: RunStore,
    fetcher: SafeFetcher,
    now: datetime | None = None,
    max_extra_pages: int = MAX_EXTRA_PAGES,
) -> EnrichOutcome:
    """Execute the enrichment stage and persist its artifacts."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    candidate_set: CandidateSet = store.read_candidates()
    sources = candidate_set.source_index()

    rows: list[CandidateEnrichment] = []
    bundles: list[PageBundle] = []
    totals: Counter[str] = Counter()
    categories: Counter[str] = Counter()

    for candidate in candidate_set.candidates:
        try:
            run = _enrich_candidate(
                candidate,
                sources=sources,
                fetcher=fetcher,
                store=store,
                max_extra=max_extra_pages,
            )
        except Exception as exc:  # noqa: BLE001 - one candidate must never fail the run.
            run = _CandidateRun(candidate=candidate)
            run.fail(
                candidate.website or "",
                FetchFailure.CONNECTION_ERROR,
                f"unexpected {type(exc).__name__} while enriching",
            )

        warnings: list[str] = []
        if not run.pages:
            warnings.append(
                "No page could be read for this company. It remains a candidate; downstream "
                "stages must treat its evidence as missing rather than negative."
            )

        bundle = PageBundle(
            company_id=candidate.company_id,
            status=run.status,
            pages=run.pages,
            sources=sorted(run.sources.values(), key=lambda s: s.source_id),
            failures=run.failures,
            generated_at=now,
            warnings=warnings,
        )
        store.write_pages(bundle)
        bundles.append(bundle)

        rows.append(
            CandidateEnrichment(
                company_id=candidate.company_id,
                status=run.status,
                website=candidate.website,
                pages_attempted=run.attempted,
                pages_extracted=len(run.pages),
                pages_deduplicated=run.deduplicated,
                failures=run.failures,
                chars_extracted=bundle.total_chars,
            )
        )

        totals[run.status.value] += 1
        totals["pages_attempted"] += run.attempted
        totals["pages_extracted"] += len(run.pages)
        totals["pages_deduplicated"] += run.deduplicated
        totals["pages_failed"] += len(run.failures)
        totals["chars_extracted"] += bundle.total_chars
        for failure in run.failures:
            categories[failure.category.value] += 1

    totals["candidates"] = len(candidate_set.candidates)
    for status in EnrichmentStatus:
        totals.setdefault(status.value, 0)

    report = EnrichmentReport(
        run_id=store.run_id,
        generated_at=now,
        candidates=rows,
        counts=dict(sorted(totals.items())),
        failures_by_category=dict(sorted(categories.items())),
        limits={
            "max_extra_pages": max_extra_pages,
            "max_text_chars": MAX_TEXT_CHARS,
            "max_response_bytes": fetcher.max_bytes,
            "max_redirects": fetcher.max_redirects,
            "connect_timeout_seconds": int(fetcher.connect_timeout),
            "read_timeout_seconds": int(fetcher.read_timeout),
        },
        notes=[
            "Candidates with no readable pages are retained. Enrichment records what could "
            "be read; it does not judge whether a company is worth reading."
        ],
    )
    report_path = store.write_enrichment_report(report)
    return EnrichOutcome(report=report, bundles=bundles, report_path=store.relative(report_path))
