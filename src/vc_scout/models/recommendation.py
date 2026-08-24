"""Research confidence and the binding recommendation.

Confidence answers "how much did we actually find out?" and is deliberately independent
of the investment score, which answers "how well does this fit the thesis?". A thin but
promising company can score well and still be low confidence; conflating the two would
hide exactly the uncertainty a partner needs to see.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from vc_scout.models.base import ArtifactModel, RecordModel
from vc_scout.models.enums import ConfidenceLevel, Recommendation
from vc_scout.util.ids import COMPANY_ID_PATTERN

__all__ = ["RecommendationResult", "ResearchConfidence"]


class ResearchConfidence(RecordModel):
    """How well-supported the analysis is, computed deterministically from coverage.

    Never supplied by a language model. ``components`` records the individual factors so
    the methodology page can show a partner exactly why confidence landed where it did.
    """

    level: ConfidenceLevel
    score: float = Field(ge=0.0, le=1.0)
    components: dict[str, float] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class RecommendationResult(ArtifactModel):
    """The final, binding call for one company.

    Produced only by :mod:`vc_scout.policy` from ``total_score`` and
    ``confidence``. ``model_suggested`` is carried alongside for auditability and has no
    influence on ``decision``.
    """

    company_id: str = Field(pattern=COMPANY_ID_PATTERN.pattern)
    decision: Recommendation
    total_score: int = Field(ge=0)
    confidence: ResearchConfidence
    policy_version: str

    band: Recommendation | None = None
    band_label: str | None = None
    capped: bool = False
    cap_reason: str | None = None
    rationale: list[str] = Field(default_factory=list)
    #: Every policy guardrail that fired, in the order it was applied.
    guardrails_applied: list[str] = Field(default_factory=list)

    #: What the analysis model would have recommended. Advisory, recorded for evaluation.
    model_suggested: Recommendation | None = None
    #: Whether the model's suggestion and the binding decision differ. Recorded so the two
    #: can be compared across runs without re-deriving either.
    model_disagreed: bool | None = None
    decided_at: datetime | None = None

    @property
    def model_agrees(self) -> bool | None:
        """Whether the model's suggestion matched the deterministic call, if it made one."""
        if self.model_suggested is None:
            return None
        return self.model_suggested is self.decision

    @model_validator(mode="after")
    def _cap_is_explained(self) -> RecommendationResult:
        if self.capped and not self.cap_reason:
            raise ValueError("a capped recommendation must record a cap_reason")
        if not self.capped and self.cap_reason:
            raise ValueError("cap_reason is only meaningful when capped is True")
        return self
