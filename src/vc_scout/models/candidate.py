"""Discovery output: a company worth spending enrichment budget on."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, field_validator

from vc_scout.models.base import ArtifactModel, RecordModel
from vc_scout.models.discovery import DiscoveryRank
from vc_scout.models.source import SourceReference, TractionSignal, is_safe_url
from vc_scout.util.ids import COMPANY_ID_PATTERN

__all__ = ["Candidate", "CandidateSet"]


class Candidate(RecordModel):
    """A startup discovered during the source stage.

    Only ``company_id``, ``name`` and at least one source are required. Everything a
    discovery source may or may not expose - a website, a one-liner, a launch date - is
    optional and stays ``None`` when absent.
    """

    company_id: str = Field(pattern=COMPANY_ID_PATTERN.pattern)
    name: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)

    one_liner: str | None = None
    website: str | None = None
    discovered_via_query: str | None = None
    discovered_at: datetime | None = None
    #: Transparent pre-analysis ordering. Never an investment signal.
    discovery_rank: DiscoveryRank | None = None
    traction_signals: list[TractionSignal] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @field_validator("website")
    @classmethod
    def _website_must_be_http(cls, value: str | None) -> str | None:
        if value is not None and not is_safe_url(value):
            raise ValueError(f"website must be an absolute http(s) URL, got {value!r}")
        return value

    @field_validator("source_ids")
    @classmethod
    def _source_ids_unique(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("source_ids must not contain duplicates")
        return value


class CandidateSet(ArtifactModel):
    """The persisted ``candidates.json`` document.

    Carries its own source table so that later stages can resolve every ``source_id``
    without re-reading raw discovery payloads.
    """

    run_id: str
    query: str
    candidates: list[Candidate] = Field(default_factory=list)
    sources: list[SourceReference] = Field(default_factory=list)
    requested_limit: int | None = Field(default=None, ge=1)
    generated_at: datetime | None = None
    notes: list[str] = Field(default_factory=list)

    def source_index(self) -> dict[str, SourceReference]:
        return {source.source_id: source for source in self.sources}

    @field_validator("candidates")
    @classmethod
    def _company_ids_unique(cls, value: list[Candidate]) -> list[Candidate]:
        ids = [candidate.company_id for candidate in value]
        if len(set(ids)) != len(ids):
            raise ValueError("candidate company_ids must be unique within a run")
        return value
