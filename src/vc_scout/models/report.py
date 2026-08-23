"""The sourcing report.

Discovery is a funnel, and a funnel that only reports its output is not auditable. This
artifact records what each query variant returned, how many hits were discarded and why,
and every individual failure - so a thin candidate set can be diagnosed rather than
guessed at.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from vc_scout.models.base import ArtifactModel, RecordModel
from vc_scout.models.enums import EnrichmentStatus, LlmErrorCategory
from vc_scout.models.page import PageFailure

__all__ = [
    "CandidateEnrichment",
    "DiscardedHit",
    "EnrichmentReport",
    "EvidenceAttempt",
    "EvidenceOutcome",
    "EvidenceReport",
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
