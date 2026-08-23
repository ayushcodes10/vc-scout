"""Scoring configuration.

This module is the single source of truth for the rubric. The dimension weights below
are the firm's, transcribed verbatim from the assignment; nothing else in the codebase
may hard-code a maximum.
"""

from __future__ import annotations

from dataclasses import dataclass

from vc_scout.models.enums import RubricDimension

__all__ = [
    "MAX_TOTAL_SCORE",
    "RUBRIC",
    "RUBRIC_BY_KEY",
    "RUBRIC_VERSION",
    "DimensionSpec",
    "max_points_for",
]

RUBRIC_VERSION = "1.0.0"


@dataclass(frozen=True, slots=True)
class DimensionSpec:
    """One scored dimension of the rubric."""

    key: RubricDimension
    title: str
    max_points: int
    description: str


RUBRIC: tuple[DimensionSpec, ...] = (
    DimensionSpec(
        key=RubricDimension.PAIN_ROI,
        title="Pain and measurable ROI",
        max_points=20,
        description=(
            "Is the workflow recurring and revenue-critical, and can the buyer measure "
            "value within 30 days?"
        ),
    ),
    DimensionSpec(
        key=RubricDimension.WEDGE,
        title="Product wedge",
        max_points=15,
        description=(
            "Is there a narrow, specific entry point that lands inside an existing system "
            "of record rather than asking for a workflow rewrite?"
        ),
    ),
    DimensionSpec(
        key=RubricDimension.DISTRIBUTION,
        title="Distribution",
        max_points=15,
        description="Is there a credible, repeatable route to SMB buyers at acceptable cost?",
    ),
    DimensionSpec(
        key=RubricDimension.DEFENSIBILITY,
        title="Defensibility",
        max_points=15,
        description=(
            "Does an advantage compound through proprietary workflow data, distribution, "
            "integrations or operational depth rather than model access alone?"
        ),
    ),
    DimensionSpec(
        key=RubricDimension.TEAM,
        title="Team",
        max_points=15,
        description=(
            "Do the founders have earned insight into this workflow and the ability to ship?"
        ),
    ),
    DimensionSpec(
        key=RubricDimension.TRACTION,
        title="Traction and freshness",
        max_points=10,
        description="Is there recent, verifiable evidence of customers, usage or revenue?",
    ),
    DimensionSpec(
        key=RubricDimension.MARKET_TIMING,
        title="Market and timing",
        max_points=10,
        description="Why is this buyable now, and why was it not buildable before?",
    ),
)

RUBRIC_BY_KEY: dict[RubricDimension, DimensionSpec] = {spec.key: spec for spec in RUBRIC}

MAX_TOTAL_SCORE = sum(spec.max_points for spec in RUBRIC)

# The rubric must cover every declared dimension exactly once and total 100 points.
# A miscount here would silently rescale every recommendation, so it fails at import.
assert len(RUBRIC_BY_KEY) == len(RUBRIC) == len(RubricDimension)
assert MAX_TOTAL_SCORE == 100


def max_points_for(dimension: RubricDimension) -> int:
    """Configured maximum for ``dimension``."""
    return RUBRIC_BY_KEY[dimension].max_points
