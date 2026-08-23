"""The deterministic recommendation policy.

Nothing in this module calls a language model, touches the network or reads the clock
beyond what it is handed. Given the same analysis and the same coverage inputs it always
returns the same recommendation - that is the point. A model may suggest a call; this is
the module that makes one.
"""

from __future__ import annotations

from datetime import datetime

from vc_scout.models.analysis import StartupAnalysis
from vc_scout.models.enums import ComponentStatus, ConfidenceLevel, Recommendation
from vc_scout.models.recommendation import RecommendationResult, ResearchConfidence
from vc_scout.rubric import RUBRIC

__all__ = [
    "CONFIDENCE_HIGH_AT",
    "CONFIDENCE_MEDIUM_AT",
    "POLICY_VERSION",
    "TAKE_A_MEETING_AT",
    "WATCH_AT",
    "band_for",
    "compute_confidence",
    "decide",
]

POLICY_VERSION = "1.0.0"

#: Score bands, inclusive lower bounds. 80-100 take a meeting, 65-79 watch, 0-64 pass.
TAKE_A_MEETING_AT = 80
WATCH_AT = 65

#: Confidence bands, inclusive lower bounds.
CONFIDENCE_HIGH_AT = 0.70
CONFIDENCE_MEDIUM_AT = 0.45

#: Confidence weights. They sum to 1.0; the assertion below keeps that true.
_W_COVERAGE = 0.45
_W_SOURCES = 0.20
_W_SITE = 0.20
_W_FRESHNESS = 0.15
assert abs(_W_COVERAGE + _W_SOURCES + _W_SITE + _W_FRESHNESS - 1.0) < 1e-9

#: Sources beyond this count no longer increase confidence.
_SOURCE_SATURATION = 4

#: Freshness decay bounds, in days.
_FRESH_UNTIL_DAYS = 90
_STALE_AFTER_DAYS = 730

#: Recommendations ordered from most to least cautious, for capping.
_RANK: dict[Recommendation, int] = {
    Recommendation.PASS: 0,
    Recommendation.WATCH: 1,
    Recommendation.TAKE_A_MEETING: 2,
}

_BAND_LABELS: dict[Recommendation, str] = {
    Recommendation.TAKE_A_MEETING: f"{TAKE_A_MEETING_AT}-100",
    Recommendation.WATCH: f"{WATCH_AT}-{TAKE_A_MEETING_AT - 1}",
    Recommendation.PASS: f"0-{WATCH_AT - 1}",
}


def band_for(total_score: int) -> Recommendation:
    """Map a total score to its band. The only place thresholds are applied."""
    if total_score >= TAKE_A_MEETING_AT:
        return Recommendation.TAKE_A_MEETING
    if total_score >= WATCH_AT:
        return Recommendation.WATCH
    return Recommendation.PASS


def _freshness(age_days: float | None) -> float:
    """Linear decay from 1.0 at 90 days old to 0.0 at 730 days old."""
    if age_days is None:
        return 0.0
    if age_days <= _FRESH_UNTIL_DAYS:
        return 1.0
    if age_days >= _STALE_AFTER_DAYS:
        return 0.0
    span = _STALE_AFTER_DAYS - _FRESH_UNTIL_DAYS
    return (_STALE_AFTER_DAYS - age_days) / span


def compute_confidence(
    analysis: StartupAnalysis,
    *,
    source_count: int,
    website_fetched: bool,
    newest_source_age_days: float | None = None,
) -> ResearchConfidence:
    """Score how well-supported an analysis is, on evidence coverage alone.

    Confidence is not a judgment about the company. It is a statement about this
    pipeline's own research: how many dimensions found evidence, how many independent
    sources were read, whether the company's own site could be read at all, and how
    recent the newest source is.
    """
    scored = [c for c in analysis.components if c.status is ComponentStatus.SCORED]
    coverage = len(scored) / len(RUBRIC)
    source_saturation = min(source_count, _SOURCE_SATURATION) / _SOURCE_SATURATION
    site = 1.0 if website_fetched else 0.0
    freshness = _freshness(newest_source_age_days)

    components = {
        "dimension_coverage": round(coverage, 4),
        "source_saturation": round(source_saturation, 4),
        "website_fetched": site,
        "freshness": round(freshness, 4),
    }
    score = (
        _W_COVERAGE * coverage
        + _W_SOURCES * source_saturation
        + _W_SITE * site
        + _W_FRESHNESS * freshness
    )
    score = round(min(max(score, 0.0), 1.0), 4)

    if score >= CONFIDENCE_HIGH_AT:
        level = ConfidenceLevel.HIGH
    elif score >= CONFIDENCE_MEDIUM_AT:
        level = ConfidenceLevel.MEDIUM
    else:
        level = ConfidenceLevel.LOW

    unknown = analysis.unknown_dimensions()
    reasons = [
        f"{len(scored)} of {len(RUBRIC)} rubric dimensions had supporting evidence.",
        f"{source_count} distinct source(s) were read.",
        (
            "The company's own website was read."
            if website_fetched
            else "The company's own website could not be read."
        ),
    ]
    if newest_source_age_days is None:
        reasons.append("No publication date was available for any source.")
    else:
        reasons.append(f"The newest source is about {int(newest_source_age_days)} days old.")

    missing = [dimension.value for dimension in unknown]
    if not website_fetched:
        missing.append("company_website")

    return ResearchConfidence(
        level=level,
        score=score,
        components=components,
        reasons=reasons,
        missing=missing,
    )


def decide(
    analysis: StartupAnalysis,
    confidence: ResearchConfidence,
    *,
    decided_at: datetime | None = None,
) -> RecommendationResult:
    """Apply the recommendation policy. This is the final call.

    ``analysis.suggested_recommendation`` is copied into the result for auditing and is
    never read as an input to the decision.
    """
    banded = band_for(analysis.total_score)
    decision = banded
    capped = False
    cap_reasons: list[str] = []

    if confidence.level is ConfidenceLevel.LOW:
        cap_reasons.append("research confidence is low")
    if "company_website" in confidence.missing:
        cap_reasons.append("the company's own website could not be read")

    if cap_reasons and _RANK[banded] > _RANK[Recommendation.WATCH]:
        decision = Recommendation.WATCH
        capped = True

    rationale = [
        f"Scored {analysis.total_score}/100 against the rubric, "
        f"which falls in the {_BAND_LABELS[banded]} band ({banded.value}).",
        f"Research confidence is {confidence.level.value} ({confidence.score:.2f}).",
    ]
    unknown = analysis.unknown_dimensions()
    if unknown:
        rationale.append(
            f"{len(unknown)} dimension(s) had no supporting evidence and were left unscored: "
            f"{', '.join(d.value for d in unknown)}. "
            f"Only {analysis.scored_out_of} of 100 points were assessable."
        )
    if capped:
        rationale.append(
            f"Capped at {Recommendation.WATCH.value} because {' and '.join(cap_reasons)}."
        )

    return RecommendationResult(
        company_id=analysis.company_id,
        decision=decision,
        total_score=analysis.total_score,
        confidence=confidence,
        policy_version=POLICY_VERSION,
        band_label=_BAND_LABELS[banded],
        capped=capped,
        cap_reason=" and ".join(cap_reasons) if capped else None,
        rationale=rationale,
        model_suggested=analysis.suggested_recommendation,
        decided_at=decided_at,
    )
