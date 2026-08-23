"""The run manifest: what ran, over what inputs, and what it produced.

This is the replay record. It deliberately stores relative paths and version identifiers
only - never absolute filesystem paths, credentials or request headers.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from vc_scout import SCHEMA_VERSION
from vc_scout.models.base import MutableModel
from vc_scout.models.enums import Recommendation, StageName, StageStatus

__all__ = ["CompanyOutcome", "RunManifest", "StageRecord"]


class StageRecord(MutableModel):
    """One pipeline stage's execution record. Mutable: filled in as the stage runs."""

    name: StageName
    status: StageStatus = StageStatus.PENDING
    started_at: datetime | None = None
    finished_at: datetime | None = None
    inputs: list[str] = Field(default_factory=list)
    outputs: list[str] = Field(default_factory=list)
    counts: dict[str, int] = Field(default_factory=dict)
    errors: list[str] = Field(default_factory=list)


class CompanyOutcome(MutableModel):
    """Per-company status, so a partial run is legible rather than mysterious."""

    company_id: str
    name: str | None = None
    reached_stage: StageName | None = None
    failed_stage: StageName | None = None
    error: str | None = None
    total_score: int | None = None
    decision: Recommendation | None = None


class RunManifest(MutableModel):
    """The persisted ``run-manifest.json`` document.

    Mutable by design: stages append to it as the run progresses. It declares
    ``schema_version`` directly rather than inheriting ``ArtifactModel``, whose records are
    frozen.
    """

    schema_version: str = SCHEMA_VERSION
    run_id: str
    query: str | None = None
    requested_limit: int | None = None

    rubric_version: str | None = None
    policy_version: str | None = None
    thesis_sha256: str | None = None
    prompt_versions: dict[str, str] = Field(default_factory=dict)
    provider: str | None = None
    model: str | None = None
    tool_version: str | None = None

    started_at: datetime | None = None
    finished_at: datetime | None = None
    stages: list[StageRecord] = Field(default_factory=list)
    companies: list[CompanyOutcome] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    def stage(self, name: StageName) -> StageRecord | None:
        for record in self.stages:
            if record.name is name:
                return record
        return None
