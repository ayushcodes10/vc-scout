"""Analysis: scored judgment about a company, every factual part of it cited.

The model that produces a :class:`StartupAnalysis` may *suggest* a recommendation, and
that suggestion is stored - but it is advisory only. The binding call is produced by
:mod:`vc_scout.policy` and lives in ``RecommendationResult``. Keeping the two in separate
fields is what makes it possible to audit how often the model and the policy disagree.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from vc_scout.models.base import ArtifactModel, RecordModel
from vc_scout.models.enums import ComponentStatus, Recommendation, RubricDimension
from vc_scout.rubric import RUBRIC, RUBRIC_BY_KEY, max_points_for
from vc_scout.util.ids import COMPANY_ID_PATTERN

__all__ = ["AnalysisSection", "RiskItem", "ScoreComponent", "StartupAnalysis"]


class AnalysisSection(RecordModel):
    """A narrative section. Factual assertions in ``text`` must be traceable, so at least
    one evidence ID is required unless the section is explicitly marked unsupported."""

    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    unsupported: bool = False

    @model_validator(mode="after")
    def _supported_sections_cite_evidence(self) -> AnalysisSection:
        if not self.unsupported and not self.evidence_ids:
            raise ValueError(
                "a supported analysis section must cite at least one evidence_id; "
                "set unsupported=True to state explicitly that evidence was not found"
            )
        return self


class RiskItem(RecordModel):
    """A risk or open concern, cited the same way as any other factual claim."""

    text: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    unsupported: bool = False

    @model_validator(mode="after")
    def _supported_risks_cite_evidence(self) -> RiskItem:
        if not self.unsupported and not self.evidence_ids:
            raise ValueError(
                "a supported risk must cite at least one evidence_id; "
                "set unsupported=True for a risk raised from absence of information"
            )
        return self


class ScoreComponent(RecordModel):
    """One dimension's contribution to the total.

    ``status`` distinguishes the two very different reasons a dimension can contribute
    zero: it was assessed and scored zero on merit, or no evidence was found at all. An
    ``unknown`` component carries ``points=None`` and contributes zero to the total while
    lowering research confidence - missing information is not evidence of weakness.
    """

    dimension: RubricDimension
    max_points: int = Field(ge=1)
    status: ComponentStatus
    points: int | None = Field(default=None, ge=0)
    rationale: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)

    @property
    def effective_points(self) -> int:
        """Points contributed to the total; ``unknown`` contributes zero."""
        return self.points or 0

    @model_validator(mode="after")
    def _respects_configured_maximum(self) -> ScoreComponent:
        configured = max_points_for(self.dimension)
        if self.max_points != configured:
            raise ValueError(
                f"{self.dimension} declares max_points={self.max_points} but the rubric "
                f"configures {configured}"
            )
        if self.points is not None and self.points > self.max_points:
            raise ValueError(
                f"{self.dimension} scored {self.points} against a configured maximum "
                f"of {self.max_points}"
            )
        if self.status is ComponentStatus.UNKNOWN:
            if self.points is not None:
                raise ValueError(f"{self.dimension} is unknown and must not carry points")
        elif self.points is None:
            raise ValueError(f"{self.dimension} is scored and must carry points")
        elif not self.evidence_ids:
            raise ValueError(f"{self.dimension} is scored and must cite at least one evidence_id")
        return self

    @classmethod
    def unknown(cls, dimension: RubricDimension, rationale: str | None = None) -> ScoreComponent:
        """An explicitly unscored dimension."""
        return cls(
            dimension=dimension,
            max_points=max_points_for(dimension),
            status=ComponentStatus.UNKNOWN,
            points=None,
            rationale=rationale,
            evidence_ids=[],
        )

    @classmethod
    def scored(
        cls,
        dimension: RubricDimension,
        points: int,
        *,
        evidence_ids: list[str],
        rationale: str | None = None,
    ) -> ScoreComponent:
        return cls(
            dimension=dimension,
            max_points=max_points_for(dimension),
            status=ComponentStatus.SCORED,
            points=points,
            rationale=rationale,
            evidence_ids=evidence_ids,
        )


class StartupAnalysis(ArtifactModel):
    """The persisted analysis for one company.

    ``total_score`` is stored rather than computed on read so that the artifact is
    self-describing, but it is validated against the component sum on every load - a
    tampered or model-supplied total cannot survive deserialisation.
    """

    company_id: str = Field(pattern=COMPANY_ID_PATTERN.pattern)
    components: list[ScoreComponent] = Field(min_length=1)
    total_score: int = Field(ge=0)
    scored_out_of: int = Field(ge=0)

    plain_language_product: str | None = None
    team: AnalysisSection | None = None
    product: AnalysisSection | None = None
    market: AnalysisSection | None = None
    risks: list[RiskItem] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    what_would_change: list[str] = Field(default_factory=list)

    #: Advisory only. Never consulted by the deterministic policy.
    suggested_recommendation: Recommendation | None = None
    suggested_recommendation_rationale: str | None = None

    model: str | None = None
    prompt_version: str | None = None
    generated_at: datetime | None = None
    warnings: list[str] = Field(default_factory=list)

    def component_index(self) -> dict[RubricDimension, ScoreComponent]:
        return {component.dimension: component for component in self.components}

    def unknown_dimensions(self) -> list[RubricDimension]:
        return [c.dimension for c in self.components if c.status is ComponentStatus.UNKNOWN]

    @model_validator(mode="after")
    def _totals_and_coverage(self) -> StartupAnalysis:
        dimensions = [component.dimension for component in self.components]
        if len(set(dimensions)) != len(dimensions):
            raise ValueError("each rubric dimension may appear at most once")
        missing = sorted(set(RUBRIC_BY_KEY) - set(dimensions))
        if missing:
            raise ValueError(
                f"analysis is missing rubric dimensions {[d.value for d in missing]}; "
                "every dimension must be present, as scored or as unknown"
            )

        expected_total = sum(component.effective_points for component in self.components)
        if self.total_score != expected_total:
            raise ValueError(
                f"total_score {self.total_score} does not equal the component sum {expected_total}"
            )

        expected_out_of = sum(
            component.max_points
            for component in self.components
            if component.status is ComponentStatus.SCORED
        )
        if self.scored_out_of != expected_out_of:
            raise ValueError(
                f"scored_out_of {self.scored_out_of} does not equal the sum of maxima for "
                f"scored components ({expected_out_of})"
            )

        if self.what_would_change and not 2 <= len(self.what_would_change) <= 3:
            raise ValueError("what_would_change must list two or three items when present")
        return self

    @classmethod
    def build(
        cls,
        *,
        company_id: str,
        components: list[ScoreComponent],
        **fields: object,
    ) -> StartupAnalysis:
        """Assemble an analysis, computing the totals from ``components``.

        Any dimension absent from ``components`` is filled in as ``unknown`` so callers
        cannot accidentally shrink the denominator.
        """
        present = {component.dimension for component in components}
        complete = list(components) + [
            ScoreComponent.unknown(spec.key) for spec in RUBRIC if spec.key not in present
        ]
        complete.sort(key=lambda component: [s.key for s in RUBRIC].index(component.dimension))
        return cls(
            company_id=company_id,
            components=complete,
            total_score=sum(component.effective_points for component in complete),
            scored_out_of=sum(
                component.max_points
                for component in complete
                if component.status is ComponentStatus.SCORED
            ),
            **fields,
        )
