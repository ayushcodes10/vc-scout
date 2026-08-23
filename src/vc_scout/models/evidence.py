"""Evidence: the layer that makes every later assertion checkable.

An :class:`EvidenceClaim` must cite at least one source *and* carry a short verbatim
excerpt from each source it cites. An :class:`EvidenceDossier` refuses to hold a claim
whose citations it cannot resolve. Those two checks are why the analysis stage can be
trusted to reference evidence IDs rather than inventing facts.

Absence is modelled as explicitly as presence. ``unknowns`` records what could not be
established and ``conflicts`` records sources that disagree - neither is a negative
finding, and both have to survive into the memo rather than being quietly dropped.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator, model_validator

from vc_scout.models.base import ArtifactModel, RecordModel
from vc_scout.models.enums import EvidenceCategory, InferenceStatus, VerificationStatus
from vc_scout.models.source import SourceReference
from vc_scout.util.ids import COMPANY_ID_PATTERN, evidence_id_for

__all__ = [
    "MAX_EXCERPT_CHARS",
    "MIN_EXCERPT_CHARS",
    "ConfidenceInputs",
    "EvidenceClaim",
    "EvidenceConflict",
    "EvidenceDossier",
    "EvidenceUnknown",
    "SourceCoverage",
    "SupportingExcerpt",
]

#: An excerpt is a citation, not a quotation of the whole page. Anything longer is either
#: the model padding or a claim too broad to be checkable.
MAX_EXCERPT_CHARS = 400
MIN_EXCERPT_CHARS = 8


class SupportingExcerpt(RecordModel):
    """A short verbatim span from one source that supports a claim.

    The excerpt is verified against the supplied source text before a dossier is written,
    so a fabricated quotation cannot survive into an artifact.
    """

    source_id: str = Field(pattern=r"^src-[0-9a-f]{12}$")
    excerpt: str = Field(min_length=MIN_EXCERPT_CHARS, max_length=MAX_EXCERPT_CHARS)


class EvidenceClaim(RecordModel):
    """One atomic, source-backed statement about a company.

    ``verification_status`` records how well-attested the claim is and
    ``inference_status`` records whether it was stated or reasoned. They are separate
    because they answer different questions: a company claim can be explicit, and an
    inference can be drawn from independently supported facts.
    """

    claim_id: str = Field(pattern=r"^ev-[0-9a-f]{12}$")
    company_id: str = Field(pattern=COMPANY_ID_PATTERN.pattern)
    category: EvidenceCategory
    claim: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)
    excerpts: list[SupportingExcerpt] = Field(min_length=1)
    verification_status: VerificationStatus
    inference_status: InferenceStatus
    caveat: str | None = None

    @field_validator("source_ids")
    @classmethod
    def _source_ids_unique(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("source_ids must not contain duplicates")
        return value

    @model_validator(mode="after")
    def _internally_consistent(self) -> EvidenceClaim:
        cited = set(self.source_ids)
        excerpted = {excerpt.source_id for excerpt in self.excerpts}
        if missing := sorted(cited - excerpted):
            raise ValueError(f"no supporting excerpt for cited source_ids {missing}")
        if extra := sorted(excerpted - cited):
            raise ValueError(f"excerpts cite source_ids {extra} that the claim does not list")
        if (
            self.verification_status is VerificationStatus.INDEPENDENTLY_SUPPORTED
            and len(cited) < 2
        ):
            raise ValueError(
                "independently_supported requires at least two separate sources; "
                f"this claim cites {len(cited)}"
            )
        expected = evidence_id_for(self.company_id, self.claim, self.source_ids)
        if self.claim_id != expected:
            raise ValueError(
                f"claim_id {self.claim_id!r} does not match its content (expected "
                f"{expected!r}); claim IDs are derived, never model-supplied"
            )
        return self

    @classmethod
    def create(
        cls,
        *,
        company_id: str,
        category: EvidenceCategory,
        claim: str,
        excerpts: list[SupportingExcerpt],
        verification_status: VerificationStatus,
        inference_status: InferenceStatus,
        caveat: str | None = None,
        source_ids: list[str] | None = None,
    ) -> EvidenceClaim:
        """Build a claim, deriving its stable ``claim_id`` from its own content."""
        ids = (
            source_ids
            if source_ids is not None
            else _ordered_unique(excerpt.source_id for excerpt in excerpts)
        )
        return cls(
            claim_id=evidence_id_for(company_id, claim, ids),
            company_id=company_id,
            category=category,
            claim=claim,
            source_ids=ids,
            excerpts=excerpts,
            verification_status=verification_status,
            inference_status=inference_status,
            caveat=caveat,
        )


def _ordered_unique(values: object) -> list[str]:
    seen: dict[str, None] = {}
    for value in values:  # type: ignore[attr-defined]
        seen.setdefault(str(value), None)
    return list(seen)


class EvidenceUnknown(RecordModel):
    """Something the sources did not establish.

    An unknown is a statement about the research, never about the company. It exists so
    that a gap stays visible instead of being read as a negative finding.
    """

    category: EvidenceCategory
    question: str = Field(min_length=1)
    reason: str | None = None


class EvidenceConflict(RecordModel):
    """Two or more sources that disagree, retained rather than resolved."""

    category: EvidenceCategory
    summary: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=2)
    excerpts: list[SupportingExcerpt] = Field(default_factory=list)


class SourceCoverage(RecordModel):
    """What material the model was actually given, and what it used.

    Persisted so that a thin dossier can be attributed to thin input rather than to a
    reluctant model.
    """

    sources_supplied: int = Field(ge=0)
    sources_cited: int = Field(ge=0)
    pages_supplied: int = Field(ge=0)
    website_available: bool = True
    hn_sources: int = Field(ge=0, default=0)
    truncated_pages: list[str] = Field(default_factory=list)
    supplied_chars: int = Field(ge=0, default=0)


class ConfidenceInputs(RecordModel):
    """Countable facts the later confidence policy consumes.

    Deliberately just counts. The policy computes confidence; this stage only reports what
    it saw, so the two cannot quietly drift into one judgment.
    """

    claims_total: int = Field(ge=0)
    claims_by_category: dict[str, int] = Field(default_factory=dict)
    claims_by_verification: dict[str, int] = Field(default_factory=dict)
    inferred_claims: int = Field(ge=0, default=0)
    unknowns: int = Field(ge=0, default=0)
    conflicts: int = Field(ge=0, default=0)
    distinct_domains: int = Field(ge=0, default=0)


class EvidenceDossier(ArtifactModel):
    """The persisted ``evidence/<company_id>.json`` document.

    Self-contained by design: it carries the sources its claims cite, so a memo can be
    re-rendered, and every citation re-checked, from this file alone.
    """

    company_id: str = Field(pattern=COMPANY_ID_PATTERN.pattern)
    claims: list[EvidenceClaim] = Field(default_factory=list)
    unknowns: list[EvidenceUnknown] = Field(default_factory=list)
    conflicts: list[EvidenceConflict] = Field(default_factory=list)
    sources: list[SourceReference] = Field(default_factory=list)
    source_coverage: SourceCoverage | None = None
    confidence_inputs: ConfidenceInputs | None = None

    prompt_version: str | None = None
    provider: str | None = None
    model: str | None = None
    generated_at: datetime | None = None
    warnings: list[str] = Field(default_factory=list)

    def source_index(self) -> dict[str, SourceReference]:
        return {source.source_id: source for source in self.sources}

    def claim_index(self) -> dict[str, EvidenceClaim]:
        return {claim.claim_id: claim for claim in self.claims}

    def claims_for(self, category: EvidenceCategory) -> list[EvidenceClaim]:
        return [claim for claim in self.claims if claim.category is category]

    @model_validator(mode="after")
    def _citations_resolve(self) -> EvidenceDossier:
        source_ids = {source.source_id for source in self.sources}
        if len(source_ids) != len(self.sources):
            raise ValueError("dossier sources must have unique source_ids")

        seen: set[str] = set()
        for claim in self.claims:
            if claim.company_id != self.company_id:
                raise ValueError(
                    f"claim {claim.claim_id} belongs to {claim.company_id!r}, "
                    f"not {self.company_id!r}"
                )
            if claim.claim_id in seen:
                raise ValueError(f"duplicate claim_id {claim.claim_id!r} in dossier")
            seen.add(claim.claim_id)
            if unresolved := sorted(set(claim.source_ids) - source_ids):
                raise ValueError(
                    f"claim {claim.claim_id} cites unknown source_ids {unresolved}; "
                    "every evidence claim must reference a source carried by the dossier"
                )

        for conflict in self.conflicts:
            if unresolved := sorted(set(conflict.source_ids) - source_ids):
                raise ValueError(f"conflict cites unknown source_ids {unresolved}")
        return self
