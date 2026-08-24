"""Builders for the recommendation stage.

Everything here is offline. No provider is constructed, no key is read and no socket is
opened - rendering reads artifacts, so its fixtures are artifacts.
"""

from __future__ import annotations

from datetime import UTC, datetime

from tests.unit.analysis_fixtures import NOW, analysis, dossier
from vc_scout.models.analysis import StartupAnalysis
from vc_scout.models.candidate import Candidate, CandidateSet
from vc_scout.models.enums import (
    AssessmentStatus,
    ConfidenceLevel,
    EnrichmentStatus,
    PageRole,
    Recommendation,
    RubricDimension,
    SourceKind,
    ThesisFit,
)
from vc_scout.models.evidence import EvidenceDossier
from vc_scout.models.page import ExtractedPage, PageBundle
from vc_scout.models.recommendation import ResearchConfidence
from vc_scout.models.report import AnalysisOutcome, AnalysisReport
from vc_scout.models.source import SourceReference
from vc_scout.policy import decide
from vc_scout.store import RunStore

__all__ = [
    "NOW",
    "unescape",
    "LOW",
    "HIGH",
    "candidate_for",
    "meeting_analysis",
    "mismatch_analysis",
    "pages_for",
    "seed_rendered_run",
    "thin_analysis",
]

LOW = ResearchConfidence(level=ConfidenceLevel.LOW, score=0.12)
HIGH = ResearchConfidence(level=ConfidenceLevel.HIGH, score=0.81)

_FOUR_UNASSESSABLE = (
    RubricDimension.DISTRIBUTION,
    RubricDimension.DEFENSIBILITY,
    RubricDimension.TRACTION,
    RubricDimension.MARKET_TIMING,
)


def candidate_for(
    bundle: EvidenceDossier,
    *,
    name: str | None = None,
    extra_source_ids: list[str] | None = None,
    include_launch: bool = True,
) -> tuple[Candidate, list[SourceReference]]:
    """A candidate whose discovery sources are the Hacker News thread and the launch page."""
    hn = SourceReference.create(
        f"https://news.ycombinator.com/item?id=70{len(bundle.company_id)}",
        kind=SourceKind.HN_STORY,
        title=f"Show HN: {bundle.company_id}",
        retrieved_at=NOW,
    )
    launch = SourceReference.create(
        f"https://{bundle.company_id}.example/pricing",
        kind=SourceKind.COMPANY_PAGE,
        title="Pricing",
        retrieved_at=NOW,
    )
    discovered = [hn, launch] if include_launch else [hn]
    candidate = Candidate(
        company_id=bundle.company_id,
        name=name or bundle.company_id.replace("-", " ").title(),
        source_ids=[source.source_id for source in discovered] + list(extra_source_ids or []),
        one_liner="AI agent for invoicing",
        website=f"https://{bundle.company_id}.example/",
    )
    return candidate, discovered


def pages_for(bundle: EvidenceDossier, *, company_id: str | None = None) -> PageBundle:
    """Extracted pages for the company pages the dossier cites."""
    roles = {0: PageRole.HOMEPAGE, 1: PageRole.PRICING}
    company = bundle.company_id
    pages = [
        ExtractedPage(
            company_id=company_id or company,
            source_id=source.source_id,
            url=source.url,
            final_url=source.url,
            text="Extracted page text.",
            content_sha256="0" * 64,
            role=roles[index],
            title=f"{company} - {roles[index].value}",
            fetched_at=NOW,
        )
        for index, source in enumerate(bundle.sources)
        if source.kind is SourceKind.COMPANY_PAGE and index in roles
    ]
    return PageBundle(
        company_id=company,
        status=EnrichmentStatus.SUCCESS,
        pages=pages,
        sources=[s for s in bundle.sources if s.kind is SourceKind.COMPANY_PAGE],
        generated_at=NOW,
    )


def thin_analysis(bundle: EvidenceDossier, *, total: int = 20) -> StartupAnalysis:
    """A low-confidence analysis whose evidence reached almost nothing.

    Produces the insufficient-evidence watch: a low band, more than three unassessable
    dimensions, low confidence, and no evidence that the company sits outside the thesis.
    """
    return analysis(
        bundle,
        total=total,
        status=AssessmentStatus.PARTIALLY_SUPPORTED,
        unassessable=_FOUR_UNASSESSABLE,
        confidence=LOW,
        thesis_verdict=ThesisFit.UNDETERMINED,
        thesis_evidence=False,
        buyer=None,
        suggested=Recommendation.PASS,
    )


def mismatch_analysis(bundle: EvidenceDossier, *, total: int = 30) -> StartupAnalysis:
    """A pass the evidence actually supports: the company sits outside the thesis."""
    return analysis(
        bundle,
        total=total,
        status=AssessmentStatus.PARTIALLY_SUPPORTED,
        confidence=HIGH,
        thesis_verdict=ThesisFit.MISMATCH,
        thesis_evidence=True,
    )


def meeting_analysis(bundle: EvidenceDossier, *, total: int = 85) -> StartupAnalysis:
    """An analysis that clears the band and every meeting requirement."""
    return analysis(
        bundle,
        total=total,
        status=AssessmentStatus.SUPPORTED,
        confidence=HIGH,
        thesis_verdict=ThesisFit.ALIGNED,
        corroborated=3,
        suggested=Recommendation.TAKE_A_MEETING,
    )


def seed_rendered_run(
    store: RunStore,
    specs: list[tuple[EvidenceDossier, StartupAnalysis | None]],
    *,
    candidates: list[Candidate] | None = None,
    sources: list[SourceReference] | None = None,
    write_pages: bool = True,
    decided_at: datetime | None = None,
) -> CandidateSet:
    """Write every artifact the recommendation stage reads.

    ``None`` in place of an analysis seeds a candidate that the analysis stage failed on -
    it appears in ``candidates.json`` and in the analysis report, and has no analysis file.
    """
    store.ensure_root()
    built: list[Candidate] = []
    source_table: list[SourceReference] = []
    outcomes: list[AnalysisOutcome] = []

    for bundle, startup in specs:
        candidate, discovery = candidate_for(bundle)
        built.append(candidate)
        source_table.extend(discovery)
        store.write_evidence(bundle)
        if write_pages:
            store.write_pages(pages_for(bundle))
        if startup is None:
            outcomes.append(AnalysisOutcome(company_id=bundle.company_id, succeeded=False))
            continue
        recommendation = decide(
            startup,
            bundle,
            startup.research_confidence,
            decided_at=decided_at or datetime(2026, 8, 26, 9, 0, tzinfo=UTC),
        )
        store.write_analysis(startup, recommendation)
        outcomes.append(
            AnalysisOutcome(
                company_id=bundle.company_id,
                succeeded=True,
                total_score=startup.total_score,
                decision=recommendation.decision,
                confidence_level=recommendation.confidence.level,
            )
        )

    candidate_set = CandidateSet(
        run_id=store.run_id,
        query="AI agents for SMB operations",
        candidates=candidates if candidates is not None else built,
        sources=sources if sources is not None else _unique(source_table),
        generated_at=NOW,
    )
    store.write_candidates(candidate_set)
    store.write_analysis_report(
        AnalysisReport(run_id=store.run_id, candidates=outcomes, generated_at=NOW)
    )
    return candidate_set


def _unique(sources: list[SourceReference]) -> list[SourceReference]:
    seen: dict[str, SourceReference] = {}
    for source in sources:
        seen.setdefault(source.source_id, source)
    return list(seen.values())


def bundles(count: int, *, claims: int = 6) -> list[EvidenceDossier]:
    """``count`` dossiers with distinct company IDs, in a stable order."""
    return [dossier(company_id=f"co-{index:02d}", claims=claims) for index in range(count)]


def unescape(value: str) -> str:
    """What survives after every backslash-escaped character is removed.

    Neutralised text keeps hostile characters visible but inert, so a test that merely
    strips backslashes would reassemble the very construct the escaping defused. Removing
    the escaped character with its backslash is what actually shows whether any live
    structure is left.
    """
    out: list[str] = []
    index = 0
    while index < len(value):
        if value[index] == "\\" and index + 1 < len(value):
            index += 2
            continue
        out.append(value[index])
        index += 1
    return "".join(out)
