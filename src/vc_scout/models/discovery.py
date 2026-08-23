"""The discovery-rank record.

The model lives here, with the other artifact records; the formula that produces it lives
in :mod:`vc_scout.discovery`. Keeping them apart lets ``Candidate`` carry a rank without
the model layer depending on discovery logic.
"""

from __future__ import annotations

from pydantic import Field

from vc_scout.models.base import RecordModel
from vc_scout.models.enums import RelevanceClass

__all__ = ["DISCOVERY_FORMULA_VERSION", "ORDERING_POLICY", "DiscoveryRank"]

#: Bumped from 1.0.0 when ranking moved from a single weighted score to a lexicographic
#: order led by topical relevance. See docs/DECISIONS.md D14.
DISCOVERY_FORMULA_VERSION = "2.0.0"

ORDERING_POLICY = (
    "lexicographic: (1) relevance class, direct before adjacent; "
    "(2) relevance score; (3) composite quality score over engagement, recency and query "
    "variant quality. Engagement can only order candidates that are already equal on class "
    "and relevance score."
)


class DiscoveryRank(RecordModel):
    """A transparent pre-analysis ordering, with every input preserved.

    There is deliberately no single composite score. Ranking is lexicographic, led by
    ``relevance_class`` and ``relevance_score``; ``quality_score`` only breaks ties between
    candidates that are already equally relevant. Collapsing these into one number is what
    previously let a high-engagement, off-topic story outrank the only on-topic one.

    This is *not* an investment score. It is computed before any page is fetched, knows
    nothing about the thesis rubric, and is never read by :mod:`vc_scout.policy`.
    """

    relevance_class: RelevanceClass
    relevance_score: float = Field(ge=0.0, le=1.0)
    quality_score: float = Field(ge=0.0, le=1.0)
    components: dict[str, float] = Field(default_factory=dict)
    #: The concept-group terms that actually matched, so a reader can check the call.
    matched: dict[str, list[str]] = Field(default_factory=dict)
    formula_version: str = DISCOVERY_FORMULA_VERSION

    @property
    def class_rank(self) -> int:
        """Ordering weight of the relevance class. Higher sorts first."""
        return {
            RelevanceClass.DIRECT: 2,
            RelevanceClass.ADJACENT: 1,
            RelevanceClass.IRRELEVANT: 0,
        }[self.relevance_class]
