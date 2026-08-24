"""The sourcing report.

Discovery is a funnel, and a funnel that only reports its output is not auditable. This
artifact records what each query variant returned, how many hits were discarded and why,
and every individual failure - so a thin candidate set can be diagnosed rather than
guessed at.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from vc_scout import SCHEMA_VERSION
from vc_scout.models.base import ArtifactModel, RecordModel
from vc_scout.models.enums import (
    ConfidenceLevel,
    EnrichmentStatus,
    LlmErrorCategory,
    Recommendation,
)
from vc_scout.models.page import PageFailure

__all__ = [
    "CandidateEnrichment",
    "DiscardedHit",
    "AnalysisAttempt",
    "AnalysisOutcome",
    "AnalysisReport",
    "EnrichmentReport",
    "EvidenceAttempt",
    "EvidenceOutcome",
    "EvidenceReport",
    "MemoFailure",
    "MemoOutcome",
    "PageFailureRecord",
    "UiReport",
    "RecommendationReport",
    "SourceReport",
    "VariantResult",
]


class VariantResult(RecordModel):
    """What one deterministic query variant returned."""

    label: str
    query: str
    tags: str
    endpoint: str
    weight: float
    hits_returned: int = 0
    pages_fetched: int = 0
    raw_paths: list[str] = Field(default_factory=list)
    error: str | None = None


class DiscardedHit(RecordModel):
    """One hit that did not become a candidate, and why."""

    reason: str
    object_id: str | None = None
    title: str | None = None
    url: str | None = None
    detail: str | None = None


class SourceReport(ArtifactModel):
    """The persisted ``source-report.json`` document."""

    run_id: str
    query: str
    requested_limit: int
    generated_at: datetime | None = None

    variants: list[VariantResult] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    discarded: list[DiscardedHit] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    #: How discovery ranked and filtered, so the shortlist can be argued with.
    formula_version: str | None = None
    ordering_policy: str | None = None
    minimum_relevance: float | None = None
    #: Relevance classes over everything that passed URL acceptance and domain dedup.
    relevance_before_selection: dict[str, int] = Field(default_factory=dict)
    #: Relevance classes of the shortlist that was actually kept.
    relevance_after_selection: dict[str, int] = Field(default_factory=dict)
    #: Requested limit minus candidates kept. Positive means the run found fewer
    #: defensible candidates than asked for, and said so rather than padding.
    shortfall: int = 0


class CandidateEnrichment(RecordModel):
    """What enrichment managed to read for one candidate."""

    company_id: str
    status: EnrichmentStatus
    website: str | None = None
    pages_attempted: int = 0
    pages_extracted: int = 0
    pages_deduplicated: int = 0
    failures: list[PageFailure] = Field(default_factory=list)
    chars_extracted: int = 0


class EnrichmentReport(ArtifactModel):
    """The persisted ``enrichment-report.json`` document.

    Records what was attempted, what was read and what failed, per candidate and in total.
    No candidate is ever dropped for having a thin or unreachable site, so this report is
    the record of which companies the analysis stage will be working blind on.
    """

    run_id: str
    generated_at: datetime | None = None
    candidates: list[CandidateEnrichment] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    failures_by_category: dict[str, int] = Field(default_factory=dict)
    #: The bounds this run was executed under, so a replay can be compared like for like.
    limits: dict[str, int] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class EvidenceAttempt(RecordModel):
    """One provider call, successful or not.

    Recorded per attempt rather than per candidate so that a retry is visible: the report
    shows what was wrong the first time and whether the second attempt fixed it.
    """

    attempt: int = Field(ge=1)
    succeeded: bool
    provider: str
    model: str | None = None
    request_id: str | None = None
    stop_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_seconds: float = 0.0
    error_category: LlmErrorCategory | None = None
    validation_errors: list[str] = Field(default_factory=list)


class EvidenceOutcome(RecordModel):
    """What evidence extraction produced for one candidate."""

    company_id: str
    succeeded: bool
    attempts: list[EvidenceAttempt] = Field(default_factory=list)
    claims: int = 0
    unknowns: int = 0
    conflicts: int = 0
    sources_supplied: int = 0
    website_available: bool = True
    truncated_sources: list[str] = Field(default_factory=list)
    error_category: LlmErrorCategory | None = None
    error_detail: str | None = None


class EvidenceReport(ArtifactModel):
    """The persisted ``evidence-report.json`` document.

    Every candidate appears, including those whose extraction failed twice. A company is
    never dropped from the run for being hard to extract evidence about.
    """

    run_id: str
    generated_at: datetime | None = None
    prompt_version: str | None = None
    prompt_sha256: str | None = None
    #: The evidence tool's output schema version. Distinct from ``schema_version``,
    #: which versions this artifact's own shape.
    output_schema_version: str | None = None
    provider: str | None = None
    model: str | None = None
    candidates: list[EvidenceOutcome] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    failures_by_category: dict[str, int] = Field(default_factory=dict)
    limits: dict[str, int] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class AnalysisAttempt(RecordModel):
    """One analysis provider call, successful or not."""

    attempt: int = Field(ge=1)
    succeeded: bool
    provider: str
    model: str | None = None
    request_id: str | None = None
    stop_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    latency_seconds: float = 0.0
    error_category: LlmErrorCategory | None = None
    validation_errors: list[str] = Field(default_factory=list)


class AnalysisOutcome(RecordModel):
    """What analysis produced for one candidate."""

    company_id: str
    succeeded: bool
    attempts: list[AnalysisAttempt] = Field(default_factory=list)
    total_score: int | None = None
    band: Recommendation | None = None
    decision: Recommendation | None = None
    model_suggested: Recommendation | None = None
    model_disagreed: bool | None = None
    guardrails_applied: list[str] = Field(default_factory=list)
    confidence_level: ConfidenceLevel | None = None
    confidence_score: float | None = None
    not_assessable: int = 0
    #: The highest total this analysis could have reached under its own assessment
    #: statuses, from the rubric ceilings. Report metadata only - it never affects the
    #: score, the confidence or the recommendation.
    maximum_achievable_score: int | None = None
    #: Whether that headroom reaches the take-a-meeting band at all. False means the band
    #: was arithmetically unreachable for this candidate on this evidence.
    meeting_reachable_by_statuses: bool | None = None
    identity_warnings: int = 0
    evidence_claims: int = 0
    error_category: LlmErrorCategory | None = None
    error_detail: str | None = None


class AnalysisReport(ArtifactModel):
    """The persisted ``analysis-report.json`` document.

    Every candidate appears, including those whose analysis failed twice. A company is never
    dropped from the run for being hard to analyse.
    """

    run_id: str
    generated_at: datetime | None = None
    thesis_version: str | None = None
    thesis_sha256: str | None = None
    prompt_version: str | None = None
    prompt_sha256: str | None = None
    output_schema_version: str | None = None
    policy_version: str | None = None
    rubric_version: str | None = None
    provider: str | None = None
    model: str | None = None
    #: Set when the run was restricted to one candidate, so a partial report can never
    #: be mistaken for a full one.
    filtered_to: str | None = None
    candidates: list[AnalysisOutcome] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    recommendations: dict[str, int] = Field(default_factory=dict)
    guardrails: dict[str, int] = Field(default_factory=dict)
    failures_by_category: dict[str, int] = Field(default_factory=dict)
    limits: dict[str, int] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class MemoOutcome(RecordModel):
    """One rendered memo, and the facts a reviewer would otherwise have to measure.

    ``words`` counts prose rather than raw tokens: a Markdown table's pipes are not words a
    partner reads, and counting them would make a scorecard-heavy memo look twice as long
    as it is.
    """

    company_id: str
    memo_path: str
    words: int = Field(ge=0)
    sources_referenced: int = Field(ge=0)
    unresolved_sources: int = Field(ge=0)
    decision: Recommendation
    total_score: int = Field(ge=0)
    maximum_achievable_score: int = Field(ge=0)
    warnings: list[str] = Field(default_factory=list)


class MemoFailure(RecordModel):
    """One candidate that produced no memo, and why.

    A failure here is per candidate and never stops the run: the other memos are still
    written, the ranking still renders, and this record - not a missing file - is how the
    gap is reported.
    """

    company_id: str
    reason: str
    detail: str | None = None


class RecommendationReport(ArtifactModel):
    """The persisted ``recommendation-report.json`` document.

    Carries no timestamp, by design. Rendering reads only validated artifacts and writes
    only deterministic output, so the same inputs produce byte-identical memos, ranking and
    report - which is what makes a re-render a check rather than a new opinion.
    """

    run_id: str
    template_version: str
    candidate_count: int = Field(ge=0)
    memos_written: int = Field(ge=0)
    ranking_path: str
    #: Ordered exactly as the ranking table presents them - triage order, not quality order.
    ordered_company_ids: list[str] = Field(default_factory=list)
    recommendations: dict[str, int] = Field(default_factory=dict)
    #: ``min`` and ``max`` of the totals rendered, absent when nothing rendered.
    score_range: dict[str, int] = Field(default_factory=dict)
    confidence_counts: dict[str, int] = Field(default_factory=dict)
    guardrail_counts: dict[str, int] = Field(default_factory=dict)
    component_status_counts: dict[str, int] = Field(default_factory=dict)
    model_policy_disagreements: int = Field(default=0, ge=0)
    #: Distinct sources cited across every memo, and how many of those could not be given
    #: a title or a URL from any artifact in the run.
    referenced_sources: int = Field(default=0, ge=0)
    missing_source_metadata: int = Field(default=0, ge=0)
    candidates_with_meeting_unreachable: int = Field(default=0, ge=0)
    memos: list[MemoOutcome] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    failures: list[MemoFailure] = Field(default_factory=list)


class PageFailureRecord(RecordModel):
    """One page the site generator could not produce, and why."""

    company_id: str
    reason: str
    detail: str | None = None


class UiReport(RecordModel):
    """The persisted ``site/ui-report.json`` document.

    Carries no timestamp, for the same reason the memos do not: the site is a rendering of
    artifacts, so an identical run must produce identical bytes and a rebuild is a check
    rather than a new result.
    """

    schema_version: str = SCHEMA_VERSION
    run_id: str
    template_version: str
    candidate_count: int = Field(ge=0)
    pages_written: int = Field(ge=0)
    company_pages: list[str] = Field(default_factory=list)
    output_paths: list[str] = Field(default_factory=list)
    removed_paths: list[str] = Field(default_factory=list)
    recommendations: dict[str, int] = Field(default_factory=dict)
    confidence_counts: dict[str, int] = Field(default_factory=dict)
    component_status_counts: dict[str, int] = Field(default_factory=dict)
    sources_cited: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list)
    failures: list[PageFailureRecord] = Field(default_factory=list)
