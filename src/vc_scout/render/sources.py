"""Reader-facing source resolution.

A memo cites ``[S1]``, not ``ev-2aff99c509bd``. The internal identifiers are how the
pipeline proves a claim is anchored; they are not how a partner reads one. This module
builds the bridge between the two, deterministically, from artifacts that are already on
disk.

Three rules shape it:

* **Nothing is invented.** A title, a URL or a date appears only if an artifact recorded
  it. Where nothing was recorded the memo says so.
* **Nothing is dropped.** A claim whose source has no display metadata still renders, with
  its internal identifier shown in the source list and a warning raised on the report. A
  memo that quietly loses a citation is worse than one that admits a gap.
* **Nothing crosses companies.** Only this company's own artifacts are read, and a record
  carrying another company's ID is refused rather than displayed.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from vc_scout.models.candidate import Candidate
from vc_scout.models.enums import PageRole, SourceKind
from vc_scout.models.evidence import EvidenceDossier
from vc_scout.models.page import ExtractedPage, PageBundle
from vc_scout.models.source import SourceReference
from vc_scout.render import markdown as md

__all__ = ["SourceEntry", "SourceIndex", "build_source_index"]

#: Cap on a supporting excerpt reproduced in a memo's source list. The excerpt is there to
#: show the citation is real, not to reproduce the page.
MAX_EXCERPT_WORDS = 13

_ROLE_LABELS: dict[PageRole, str] = {
    PageRole.HOMEPAGE: "company homepage",
    PageRole.LAUNCH: "launch page posted to Hacker News",
    PageRole.PRODUCT: "company product page",
    PageRole.PRICING: "company pricing page",
    PageRole.CUSTOMERS: "company customers page",
    PageRole.ABOUT: "company about page",
    PageRole.TEAM: "company team page",
    PageRole.CHANGELOG: "company changelog",
    PageRole.BLOG: "company blog",
}

_KIND_LABELS: dict[SourceKind, str] = {
    SourceKind.HN_STORY: "Hacker News launch thread",
    SourceKind.HN_COMMENTS: "Hacker News discussion",
    SourceKind.COMPANY_PAGE: "company page",
    SourceKind.OTHER: "web page",
}

#: Shown in place of a title, a URL or a date that no artifact recorded.
UNAVAILABLE = "Source metadata unavailable"


@dataclass(frozen=True, slots=True)
class SourceEntry:
    """One reader-facing source, assembled from whatever the run actually recorded."""

    source_id: str
    resolved: bool
    title: str | None = None
    url: str | None = None
    role_label: str = "unrecorded source"
    observed_on: str | None = None
    excerpt: str | None = None


@dataclass(slots=True)
class SourceIndex:
    """Resolved sources for one company, plus the markers a memo has spent.

    Markers are assigned on first reference, in the order the memo asks for them, so the
    numbering follows the reading order of the document rather than any internal ordering.
    """

    company_id: str
    entries: dict[str, SourceEntry]
    warnings: list[str] = field(default_factory=list)
    _markers: dict[str, str] = field(default_factory=dict)

    def marker(self, source_id: str) -> str:
        """The marker for ``source_id``, assigning the next one on first use."""
        if source_id not in self._markers:
            self._markers[source_id] = f"S{len(self._markers) + 1}"
            if source_id not in self.entries:
                self.entries[source_id] = SourceEntry(source_id=source_id, resolved=False)
                self.warnings.append(
                    f"{self.company_id}: source {source_id} is cited by the evidence but no "
                    "artifact in this run records a title or URL for it; the citation is "
                    "rendered with its internal identifier"
                )
        return self._markers[source_id]

    def markers_for(self, source_ids: list[str]) -> list[str]:
        """Markers for ``source_ids``, deduplicated, in the order supplied."""
        seen: dict[str, None] = {}
        for source_id in source_ids:
            seen.setdefault(self.marker(source_id), None)
        return list(seen)

    def cited(self) -> list[tuple[str, SourceEntry]]:
        """Every marker actually spent, in assignment order."""
        return [(marker, self.entries[sid]) for sid, marker in self._markers.items()]

    @property
    def unresolved_count(self) -> int:
        return sum(1 for _, entry in self.cited() if not entry.resolved)


def _observed_on(source: SourceReference | None, page_iso: str | None) -> str | None:
    """The date a source was observed, as ``YYYY-MM-DD``. Never guessed."""
    if page_iso:
        return page_iso
    if source is not None:
        for stamp in (source.retrieved_at, source.published_at):
            if stamp is not None:
                return stamp.date().isoformat()
    return None


def _excerpt_for(dossier: EvidenceDossier, source_id: str) -> str | None:
    """The first recorded excerpt supporting a claim from this source, truncated."""
    for claim in dossier.claims:
        for excerpt in claim.excerpts:
            if excerpt.source_id == source_id:
                return md.truncate_words(excerpt.excerpt, MAX_EXCERPT_WORDS)
    return None


def build_source_index(
    company_id: str,
    *,
    candidate: Candidate | None,
    candidate_sources: dict[str, SourceReference],
    pages: PageBundle | None,
    dossier: EvidenceDossier,
) -> SourceIndex:
    """Assemble every source this company's memo could cite.

    Precedence for the URL and the title is extracted page metadata first - it records the
    URL the fetch actually landed on after redirects, and the page's own title - then the
    dossier's source table, then the discovery source table. A Hacker News thread and a
    launch page are separate sources and stay separate: they are different documents making
    different kinds of statement.
    """
    warnings: list[str] = []
    references: dict[str, SourceReference] = {}

    def offer(source: SourceReference) -> None:
        references.setdefault(source.source_id, source)

    for source in dossier.sources:
        offer(source)
    if pages is not None:
        if pages.company_id != company_id:
            warnings.append(
                f"{company_id}: refusing extracted pages belonging to {pages.company_id!r}"
            )
            pages = None
        else:
            for source in pages.sources:
                offer(source)
    for source_id in candidate.source_ids if candidate else []:
        if discovered := candidate_sources.get(source_id):
            offer(discovered)

    page_index: dict[str, ExtractedPage] = {}
    for page in pages.pages if pages else []:
        if page.company_id != company_id:
            # Cannot happen through the store, which writes one bundle per company - but a
            # memo is the last place a mixed-up artifact should be able to surface.
            warnings.append(
                f"{company_id}: refusing extracted page {page.source_id} recorded against "
                f"{page.company_id!r}"
            )
            continue
        page_index[page.source_id] = page

    entries: dict[str, SourceEntry] = {}
    for source_id in sorted(set(references) | set(page_index)):
        reference = references.get(source_id)
        extracted = page_index.get(source_id)
        url = (extracted.final_url if extracted else None) or (reference.url if reference else None)
        title = (extracted.title if extracted else None) or (reference.title if reference else None)
        if extracted is not None and extracted.role is not None:
            role_label = _ROLE_LABELS[extracted.role]
        elif reference is not None:
            role_label = _KIND_LABELS[reference.kind]
        else:
            role_label = "unrecorded source"
        observed = (
            extracted.fetched_at.date().isoformat() if extracted and extracted.fetched_at else None
        )
        entries[source_id] = SourceEntry(
            source_id=source_id,
            resolved=bool(url),
            title=title,
            url=url,
            role_label=role_label,
            observed_on=_observed_on(reference, observed),
            excerpt=_excerpt_for(dossier, source_id),
        )

    return SourceIndex(company_id=company_id, entries=entries, warnings=warnings)
