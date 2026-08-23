"""The deterministic policy.

These are the tests that make the claim "an LLM cannot make the final recommendation"
verifiable rather than aspirational.
"""

from __future__ import annotations

import pytest

from tests.unit import factories
from vc_scout.models.enums import ConfidenceLevel, Recommendation
from vc_scout.models.recommendation import ResearchConfidence
from vc_scout.policy import POLICY_VERSION, band_for, compute_confidence, decide


def confident() -> ResearchConfidence:
    """Confidence high enough that no cap applies."""
    return ResearchConfidence(level=ConfidenceLevel.HIGH, score=0.9, missing=[])


@pytest.mark.parametrize(
    ("score", "expected"),
    [
        (0, Recommendation.PASS),
        (64, Recommendation.PASS),
        (65, Recommendation.WATCH),
        (79, Recommendation.WATCH),
        (80, Recommendation.TAKE_A_MEETING),
        (100, Recommendation.TAKE_A_MEETING),
    ],
)
def test_band_boundaries(score: int, expected: Recommendation) -> None:
    assert band_for(score) is expected


@pytest.mark.parametrize(
    ("score", "expected"),
    [(64, Recommendation.PASS), (65, Recommendation.WATCH), (80, Recommendation.TAKE_A_MEETING)],
)
def test_decide_follows_the_bands(score: int, expected: Recommendation) -> None:
    result = decide(factories.analysis_scoring(score), confident())
    assert result.decision is expected
    assert result.capped is False
    assert result.policy_version == POLICY_VERSION


def test_low_confidence_caps_a_meeting_down_to_watch() -> None:
    low = ResearchConfidence(level=ConfidenceLevel.LOW, score=0.2)
    result = decide(factories.analysis_scoring(90), low)
    assert result.decision is Recommendation.WATCH
    assert result.capped is True
    assert result.cap_reason is not None
    assert "confidence" in result.cap_reason


def test_unreadable_website_caps_a_meeting_down_to_watch() -> None:
    blind = ResearchConfidence(level=ConfidenceLevel.HIGH, score=0.8, missing=["company_website"])
    result = decide(factories.analysis_scoring(95), blind)
    assert result.decision is Recommendation.WATCH
    assert result.capped is True


def test_capping_never_upgrades_a_pass() -> None:
    low = ResearchConfidence(level=ConfidenceLevel.LOW, score=0.1)
    result = decide(factories.analysis_scoring(10), low)
    assert result.decision is Recommendation.PASS
    assert result.capped is False


def test_model_suggestion_is_recorded_but_never_obeyed() -> None:
    analysis = factories.analysis_scoring(
        10, suggested_recommendation=Recommendation.TAKE_A_MEETING
    )
    result = decide(analysis, confident())
    assert result.model_suggested is Recommendation.TAKE_A_MEETING
    assert result.decision is Recommendation.PASS
    assert result.model_agrees is False


def test_model_agreement_is_none_when_no_suggestion_was_made() -> None:
    assert decide(factories.analysis_scoring(70), confident()).model_agrees is None


def test_decision_is_identical_for_every_possible_model_suggestion() -> None:
    """The suggestion is an input the policy must be blind to."""
    decisions = {
        decide(
            factories.analysis_scoring(70, suggested_recommendation=suggestion), confident()
        ).decision
        for suggestion in [*Recommendation, None]
    }
    assert decisions == {Recommendation.WATCH}


def test_rationale_reports_unassessable_points() -> None:
    analysis = factories.analysis_scoring(20)  # only pain_roi scored
    result = decide(analysis, confident())
    assert any("Only 20 of 100 points were assessable" in line for line in result.rationale)


# -- confidence --------------------------------------------------------------


def test_confidence_is_independent_of_score() -> None:
    """Identical coverage must produce identical confidence, whatever the score."""
    weak = factories.analysis_full_coverage(0.1)
    strong = factories.analysis_full_coverage(1.0)
    assert weak.total_score < strong.total_score

    kwargs = {"source_count": 4, "website_fetched": True, "newest_source_age_days": 10.0}
    assert compute_confidence(weak, **kwargs).score == compute_confidence(strong, **kwargs).score


def test_full_coverage_and_fresh_sources_yield_high_confidence() -> None:
    result = compute_confidence(
        factories.analysis_scoring(100),
        source_count=4,
        website_fetched=True,
        newest_source_age_days=5.0,
    )
    assert result.level is ConfidenceLevel.HIGH
    assert result.score == pytest.approx(1.0)
    assert result.missing == []


def test_thin_research_yields_low_confidence_and_names_what_is_missing() -> None:
    result = compute_confidence(
        factories.analysis_scoring(20),  # pain_roi only
        source_count=1,
        website_fetched=False,
        newest_source_age_days=None,
    )
    assert result.level is ConfidenceLevel.LOW
    assert "company_website" in result.missing
    assert "team" in result.missing


def test_stale_sources_reduce_confidence() -> None:
    fresh = compute_confidence(
        factories.analysis_scoring(100),
        source_count=4,
        website_fetched=True,
        newest_source_age_days=30.0,
    )
    stale = compute_confidence(
        factories.analysis_scoring(100),
        source_count=4,
        website_fetched=True,
        newest_source_age_days=900.0,
    )
    assert stale.score < fresh.score


def test_confidence_stays_within_bounds() -> None:
    for sources in (0, 1, 4, 40):
        result = compute_confidence(
            factories.analysis_scoring(0),
            source_count=sources,
            website_fetched=False,
            newest_source_age_days=None,
        )
        assert 0.0 <= result.score <= 1.0
