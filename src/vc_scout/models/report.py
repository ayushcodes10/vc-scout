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
from vc_scout.models.enums import EnrichmentStatus
from vc_scout.models.page import PageFailure

__all__ = [
    "CandidateEnrichment",
    "DiscardedHit",
    "EnrichmentReport",
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
