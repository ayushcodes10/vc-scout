"""Analysis: an evidence-bound investment case, scored against the rubric.

The score measures **the strength of the evidence-backed investment case**, not the
company's objective worth. That distinction runs through every model here: a dimension with
nothing behind it is ``not_assessable`` and capped, never silently zeroed and never treated
as a finding against the company.

A language model produces the narrative, the per-dimension assessment and a *suggested*
recommendation. It does not produce the total (recomputed in Python), the research
confidence (computed deterministically from coverage) or the binding recommendation (made
by :mod:`vc_scout.policy`). Those three separations are what the artifact contract enforces.
"""

from __future__ import annotations

import math
from datetime import datetime

from pydantic import Field, model_validator

from vc_scout.models.base import ArtifactModel, RecordModel
from vc_scout.models.enums import AssessmentStatus, Recommendation, RubricDimension, ThesisFit
from vc_scout.models.recommendation import ResearchConfidence
from vc_scout.rubric import RUBRIC, RUBRIC_BY_KEY, max_points_for
from vc_scout.util.ids import COMPANY_ID_PATTERN

__all__ = [
    "MAX_RECOMMENDATION_CHANGERS",
    "MIN_RECOMMENDATION_CHANGERS",
    "STATUS_CEILING_RATIO",
    "AnalysisSection",
    "CompetitiveObservation",
    "CorroboratedFinding",
    "RiskItem",
    "ScoreComponent",
    "StartupAnalysis",
    "ThesisAssessment",
    "ceiling_for",
]

#: The share of a dimension's maximum that each assessment status may reach.
#:
#: ``supported`` gets the full range. ``partially_supported`` and ``not_assessable`` are
#: capped so that a confident-sounding narrative cannot earn points the evidence does not
#: carry. ``contradicted`` is deliberately uncapped: contrary evidence is *evidence*, and
#: what matters is that the rationale explains it, which is validated separately.
STATUS_CEILING_RATIO: dict[AssessmentStatus, float] = {
    AssessmentStatus.SUPPORTED: 1.00,
    AssessmentStatus.PARTIALLY_SUPPORTED: 0.70,
    AssessmentStatus.CONTRADICTED: 1.00,
    AssessmentStatus.NOT_ASSESSABLE: 0.50,
}

MIN_RECOMMENDATION_CHANGERS = 2
MAX_RECOMMENDATION_CHANGERS = 3


def ceiling_for(dimension: RubricDimension, status: AssessmentStatus) -> int:
    """Highest score a dimension may receive under ``status``.

    Floored, so a ratio that lands between whole points can never round *up* into points
    the evidence did not support.
    """
    return math.floor(max_points_for(dimension) * STATUS_CEILING_RATIO[status])


class AnalysisSection(RecordModel):
    """A narrative section of the analysis.

    Must be anchored: either it cites evidence, or it cites the recorded unknowns it is
    reasoning about. A section that does neither is an unsourced assertion.
    """

    text: str = Field(min_length=1)
    evidence_claim_ids: list[str] = Field(default_factory=list)
    unknown_references: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _is_anchored(self) -> AnalysisSection:
        if not self.evidence_claim_ids and not self.unknown_references:
            raise ValueError(
                "an analysis section must cite evidence claim IDs, or name the recorded "
                "unknowns it is reasoning from"
            )
        return self


class RiskItem(RecordModel):
    """A risk. Either evidence shows it, or a recorded unknown gives rise to it."""

    text: str = Field(min_length=1)
    evidence_claim_ids: list[str] = Field(default_factory=list)
    unknown_references: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _is_anchored(self) -> RiskItem:
        if not self.evidence_claim_ids and not self.unknown_references:
            raise ValueError(
                "a risk must cite evidence claim IDs, or state the recorded unknown it arises from"
            )
        return self


class CompetitiveObservation(RecordModel):
    """An observation about competition.

    Requires evidence: a competitor may only be named when a supplied claim names it, so
    an observation with nothing behind it cannot exist.
    """

    text: str = Field(min_length=1)
    evidence_claim_ids: list[str] = Field(min_length=1)


class CorroboratedFinding(RecordModel):
    """A fact the analysis judges to be genuinely corroborated.

    The ``independently_supported`` label on an evidence claim is a mechanical property -
    two cited sources - and the first live run produced one that was really two sources
    supporting different halves of a compound statement. Corroboration therefore has to be
    asserted here, about a *named fact*, and only these findings count towards research
    confidence. The label alone earns nothing.
    """

    fact: str = Field(min_length=1)
    evidence_claim_ids: list[str] = Field(min_length=1)


class ThesisAssessment(RecordModel):
    """Where the evidence places the company relative to the firm's thesis.

    ``mismatch`` is a positive finding - developer infrastructure, a personal project, an
    enterprise platform - and must cite the evidence showing it. ``undetermined`` is the
    honest answer when nothing establishes fit either way.
    """

    verdict: ThesisFit
    rationale: str = Field(min_length=1)
    evidence_claim_ids: list[str] = Field(default_factory=list)
    unknown_references: list[str] = Field(default_factory=list)

    @property
    def is_supported_mismatch(self) -> bool:
        """A thesis mismatch the evidence actually shows, rather than an absence of fit."""
        return self.verdict is ThesisFit.MISMATCH and bool(self.evidence_claim_ids)

    @model_validator(mode="after")
    def _mismatch_requires_evidence(self) -> ThesisAssessment:
        if self.verdict is ThesisFit.MISMATCH and not self.evidence_claim_ids:
            raise ValueError(
                "a thesis mismatch is a finding about the company and must cite evidence; "
                "use 'undetermined' when the sources do not establish fit"
            )
        return self


class ScoreComponent(RecordModel):
    """One rubric dimension's contribution to the total."""

    component: RubricDimension
    score: int = Field(ge=0)
    maximum: int = Field(ge=1)
    assessment_status: AssessmentStatus
    rationale: str = Field(min_length=1)
    evidence_claim_ids: list[str] = Field(default_factory=list)
    unknown_references: list[str] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)

    @property
    def ceiling(self) -> int:
        return ceiling_for(self.component, self.assessment_status)

    @property
    def has_evidence(self) -> bool:
        return bool(self.evidence_claim_ids)

    @model_validator(mode="after")
    def _respects_the_rubric_and_its_ceiling(self) -> ScoreComponent:
        configured = max_points_for(self.component)
        if self.maximum != configured:
            raise ValueError(
                f"{self.component.value} declares maximum={self.maximum} but the rubric "
                f"configures {configured}"
            )
        if self.score > self.maximum:
            raise ValueError(
                f"{self.component.value} scored {self.score} against a maximum of {self.maximum}"
            )
        if self.score > self.ceiling:
            raise ValueError(
                f"{self.component.value} is {self.assessment_status.value} and may score at "
                f"most {self.ceiling} of {self.maximum}; got {self.score}"
            )
        if self.assessment_status is AssessmentStatus.CONTRADICTED and not self.evidence_claim_ids:
            raise ValueError(
                f"{self.component.value} is contradicted, which is a finding and must cite "
                "the contrary evidence"
            )
        if (
            self.assessment_status
            in (AssessmentStatus.SUPPORTED, AssessmentStatus.PARTIALLY_SUPPORTED)
            and not self.evidence_claim_ids
        ):
            raise ValueError(
                f"{self.component.value} is {self.assessment_status.value} and must cite at "
                "least one evidence claim ID"
            )
        return self


class StartupAnalysis(ArtifactModel):
    """The persisted analysis for one company.

    ``total_score`` is stored so the artifact is self-describing, and revalidated against
    the component sum on every load - a tampered or model-supplied total cannot survive
    deserialisation. ``research_confidence`` is computed by the policy, never by the model.
    """

    company_id: str = Field(pattern=COMPANY_ID_PATTERN.pattern)
    thesis_version: str
    prompt_version: str
    provider: str
    model: str

    plain_language_product: str = Field(min_length=1)
    buyer: str | None = None
    workflow: str | None = None

    team_assessment: AnalysisSection
    product_assessment: AnalysisSection
    market_assessment: AnalysisSection
    thesis_assessment: ThesisAssessment
    competitive_observations: list[CompetitiveObservation] = Field(default_factory=list)
    corroborated_findings: list[CorroboratedFinding] = Field(default_factory=list)
    risks: list[RiskItem] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)

    score_components: list[ScoreComponent] = Field(min_length=1)
    total_score: int = Field(ge=0)

    research_confidence: ResearchConfidence
    confidence_rationale: list[str] = Field(default_factory=list)

    #: Advisory only. Never consulted by the deterministic policy.
    model_suggested_recommendation: Recommendation | None = None
    recommendation_changers: list[str] = Field(default_factory=list)

    identity_warnings: list[str] = Field(default_factory=list)
    analysis_warnings: list[str] = Field(default_factory=list)
    generated_at: datetime | None = None

    def component_index(self) -> dict[RubricDimension, ScoreComponent]:
        return {component.component: component for component in self.score_components}

    def components_with_status(self, status: AssessmentStatus) -> list[ScoreComponent]:
        return [c for c in self.score_components if c.assessment_status is status]

    def dimensions_with_evidence(self) -> list[RubricDimension]:
        """Dimensions the evidence could actually speak to."""
        return [
            c.component
            for c in self.score_components
            if c.has_evidence
            and c.assessment_status
            in (
                AssessmentStatus.SUPPORTED,
                AssessmentStatus.PARTIALLY_SUPPORTED,
                AssessmentStatus.CONTRADICTED,
            )
        ]

    @property
    def scored_out_of(self) -> int:
        """Points that were actually assessable, so a low total can be read correctly."""
        return sum(
            c.maximum
            for c in self.score_components
            if c.assessment_status is not AssessmentStatus.NOT_ASSESSABLE
        )

    @model_validator(mode="after")
    def _rubric_is_complete_and_total_is_arithmetic(self) -> StartupAnalysis:
        dimensions = [component.component for component in self.score_components]
        if len(set(dimensions)) != len(dimensions):
            raise ValueError("each rubric dimension may appear at most once")
        if missing := sorted(set(RUBRIC_BY_KEY) - set(dimensions)):
            raise ValueError(
                f"analysis is missing rubric dimensions {[d.value for d in missing]}; all "
                f"{len(RUBRIC)} must be present"
            )

        expected = sum(component.score for component in self.score_components)
        if self.total_score != expected:
            raise ValueError(
                f"total_score {self.total_score} does not equal the component sum {expected}"
            )

        if not (
            MIN_RECOMMENDATION_CHANGERS
            <= len(self.recommendation_changers)
            <= MAX_RECOMMENDATION_CHANGERS
        ):
            raise ValueError(
                f"recommendation_changers must list {MIN_RECOMMENDATION_CHANGERS} or "
                f"{MAX_RECOMMENDATION_CHANGERS} items; got {len(self.recommendation_changers)}"
            )
        return self
