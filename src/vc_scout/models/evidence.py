"""Evidence: the layer that makes every later assertion checkable.

An :class:`EvidenceClaim` must cite at least one source, and an :class:`EvidenceDossier`
refuses to hold a claim whose citations it cannot resolve. That check is the reason the
analysis stage can be trusted to cite evidence IDs rather than inventing facts.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator, model_validator

from vc_scout.models.base import ArtifactModel, RecordModel
from vc_scout.models.enums import ClaimLabel, RubricDimension
from vc_scout.models.source import SourceReference
from vc_scout.util.ids import COMPANY_ID_PATTERN, evidence_id_for

__all__ = ["EvidenceClaim", "EvidenceDossier"]


class EvidenceClaim(RecordModel):
    """One atomic, source-backed statement about a company.

    ``label`` records provenance: a claim taken from the company's own marketing is a
    ``company_claim`` and must never be presented as verified fact; a conclusion drawn by
    the model is an ``inference``.
    """

    evidence_id: str = Field(pattern=r"^ev-[0-9a-f]{12}$")
    company_id: str = Field(pattern=COMPANY_ID_PATTERN.pattern)
    claim: str = Field(min_length=1)
    label: ClaimLabel
    source_ids: list[str] = Field(min_length=1)

    dimension: RubricDimension | None = None
    quote: str | None = None
    observed_at: datetime | None = None
    notes: str | None = None

    @field_validator("source_ids")
    @classmethod
    def _source_ids_unique(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("source_ids must not contain duplicates")
        return value

    @model_validator(mode="after")
    def _evidence_id_matches_content(self) -> EvidenceClaim:
        expected = evidence_id_for(self.company_id, self.claim, self.source_ids)
        if self.evidence_id != expected:
            raise ValueError(
                f"evidence_id {self.evidence_id!r} does not match its content "
                f"(expected {expected!r}); evidence IDs must be content-derived"
            )
        return self

    @classmethod
    def create(
        cls,
        *,
        company_id: str,
        claim: str,
        label: ClaimLabel,
        source_ids: list[str],
        dimension: RubricDimension | None = None,
        quote: str | None = None,
        observed_at: datetime | None = None,
        notes: str | None = None,
    ) -> EvidenceClaim:
        """Build a claim, deriving its stable ``evidence_id``."""
        return cls(
            evidence_id=evidence_id_for(company_id, claim, source_ids),
            company_id=company_id,
            claim=claim,
            label=label,
            source_ids=source_ids,
            dimension=dimension,
            quote=quote,
            observed_at=observed_at,
            notes=notes,
        )


class EvidenceDossier(ArtifactModel):
    """The persisted ``evidence/<company_id>.json`` document.

    Self-contained by design: it carries the sources its claims cite, so a memo can be
    re-rendered from this file alone.
    """

    company_id: str = Field(pattern=COMPANY_ID_PATTERN.pattern)
    claims: list[EvidenceClaim] = Field(default_factory=list)
    sources: list[SourceReference] = Field(default_factory=list)

    prompt_version: str | None = None
    model: str | None = None
    generated_at: datetime | None = None
    warnings: list[str] = Field(default_factory=list)

    def source_index(self) -> dict[str, SourceReference]:
        return {source.source_id: source for source in self.sources}

    def claim_index(self) -> dict[str, EvidenceClaim]:
        return {claim.evidence_id: claim for claim in self.claims}

    def claims_for(self, dimension: RubricDimension) -> list[EvidenceClaim]:
        return [claim for claim in self.claims if claim.dimension == dimension]

    @model_validator(mode="after")
    def _citations_resolve(self) -> EvidenceDossier:
        source_ids = {source.source_id for source in self.sources}
        if len(source_ids) != len(self.sources):
            raise ValueError("dossier sources must have unique source_ids")

        seen: set[str] = set()
        for claim in self.claims:
            if claim.company_id != self.company_id:
                raise ValueError(
                    f"claim {claim.evidence_id} belongs to {claim.company_id!r}, "
                    f"not {self.company_id!r}"
                )
            if claim.evidence_id in seen:
                raise ValueError(f"duplicate evidence_id {claim.evidence_id!r} in dossier")
            seen.add(claim.evidence_id)

            unresolved = sorted(set(claim.source_ids) - source_ids)
            if unresolved:
                raise ValueError(
                    f"claim {claim.evidence_id} cites unknown source_ids {unresolved}; "
                    "every evidence claim must reference a source carried by the dossier"
                )
        return self
