"""Research confidence and the deterministic recommendation policy.

These are the tests that make "an LLM cannot make the final recommendation" and "missing
evidence is not evidence of weakness" verifiable rather than aspirational.
"""

from __future__ import annotations

import re

import pytest

from tests.unit.analysis_fixtures import analysis, dossier
from vc_scout.models.analysis import ceiling_for
from vc_scout.models.enums import (
    AssessmentStatus,
    ConfidenceLevel,
    EvidenceCategory,
    Recommendation,
    RubricDimension,
    ThesisFit,
    VerificationStatus,
)
from vc_scout.models.recommendation import ResearchConfidence
from vc_scout.policy import (
    CONFIDENCE_HIGH_AT,
    CONFIDENCE_MEDIUM_AT,
    POLICY_VERSION,
    Guardrail,
    band_for,
    compute_confidence,
    decide,
)
from vc_scout.rubric import RUBRIC

ALL_DIMENSIONS = tuple(RubricDimension)


def confident(level: ConfidenceLevel = ConfidenceLevel.HIGH, score: float = 0.9):
    return ResearchConfidence(level=level, score=score)


# -- bands -------------------------------------------------------------------


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
    ("total", "expected"),
    [(50, Recommendation.PASS), (70, Recommendation.WATCH), (90, Recommendation.TAKE_A_MEETING)],
)
def test_decide_follows_the_bands_when_no_guardrail_fires(
    total: int, expected: Recommendation
) -> None:
    bundle = dossier(claims=8)
    result = decide(analysis(bundle, total=total), bundle, confident())
    assert result.decision is expected
    assert result.band is band_for(total)
    assert result.policy_version == POLICY_VERSION
    assert result.guardrails_applied == []


# -- take-a-meeting guardrails ----------------------------------------------


def test_a_meeting_requires_at_least_medium_confidence() -> None:
    bundle = dossier(claims=8)
    result = decide(analysis(bundle, total=90), bundle, confident(ConfidenceLevel.LOW, 0.2))
    assert result.decision is Recommendation.WATCH
    assert Guardrail.MEETING_NEEDS_CONFIDENCE in result.guardrails_applied
    assert result.capped is True


def test_a_meeting_requires_an_identifiable_buyer() -> None:
    bundle = dossier(claims=8)
    result = decide(analysis(bundle, total=90, buyer=None), bundle, confident())
    assert result.decision is Recommendation.WATCH
    assert Guardrail.MEETING_NEEDS_BUYER in result.guardrails_applied


def test_a_meeting_needs_evidence_in_four_dimensions_and_four_is_enough() -> None:
    """The boundary the guardrail defends."""
    bundle = dossier(claims=8)
    four_evidenced = analysis(
        bundle,
        total=82,
        unassessable=(
            RubricDimension.TEAM,
            RubricDimension.TRACTION,
            RubricDimension.MARKET_TIMING,
        ),
    )
    assert len(four_evidenced.dimensions_with_evidence()) == 4
    result = decide(four_evidenced, bundle, confident())
    assert result.decision is Recommendation.TAKE_A_MEETING
    assert Guardrail.MEETING_NEEDS_BREADTH not in result.guardrails_applied


def test_three_evidenced_dimensions_cannot_reach_the_meeting_band_at_all() -> None:
    """The breadth guardrail is defence in depth, not the mechanism.

    With four dimensions unassessable, the status ceilings cap the achievable total below
    the meeting threshold, so a narrow-evidence analysis can never reach that band. This
    pins the arithmetic rather than asserting it from memory.
    """
    unassessable = (
        RubricDimension.DEFENSIBILITY,
        RubricDimension.TEAM,
        RubricDimension.TRACTION,
        RubricDimension.MARKET_TIMING,
    )
    highest = sum(
        ceiling_for(
            spec.key,
            AssessmentStatus.NOT_ASSESSABLE
            if spec.key in unassessable
            else AssessmentStatus.SUPPORTED,
        )
        for spec in RUBRIC
    )
    assert highest < 80, f"three evidenced dimensions can reach {highest}"

    bundle = dossier(claims=8)
    narrow = analysis(bundle, total=highest, unassessable=unassessable)
    assert len(narrow.dimensions_with_evidence()) == 3
    assert decide(narrow, bundle, confident()).decision is not Recommendation.TAKE_A_MEETING


def test_a_meeting_survives_when_every_requirement_is_met() -> None:
    bundle = dossier(claims=8)
    result = decide(analysis(bundle, total=85), bundle, confident())
    assert result.decision is Recommendation.TAKE_A_MEETING
    assert result.capped is False


# -- insufficient evidence ---------------------------------------------------


def test_a_low_score_driven_by_missing_evidence_becomes_watch_not_pass() -> None:
    """The guardrail that stops an evidence shortfall from reading as a judgment."""
    bundle = dossier(claims=1)
    thin = analysis(
        bundle,
        total=10,
        unassessable=(
            RubricDimension.DISTRIBUTION,
            RubricDimension.DEFENSIBILITY,
            RubricDimension.TEAM,
            RubricDimension.TRACTION,
            RubricDimension.MARKET_TIMING,
        ),
        thesis_verdict=ThesisFit.UNDETERMINED,
        thesis_evidence=False,
    )
    result = decide(thin, bundle, confident(ConfidenceLevel.LOW, 0.2))
    assert result.decision is Recommendation.WATCH
    assert Guardrail.INSUFFICIENT_EVIDENCE in result.guardrails_applied
    assert any("evidence shortfall, not a judgment" in line for line in result.rationale)


def test_a_supported_thesis_mismatch_may_pass_even_at_low_confidence() -> None:
    """Positive evidence that a company is outside the thesis is a real finding."""
    bundle = dossier(claims=1)
    mismatch = analysis(
        bundle,
        total=10,
        unassessable=(
            RubricDimension.DISTRIBUTION,
            RubricDimension.DEFENSIBILITY,
            RubricDimension.TEAM,
            RubricDimension.TRACTION,
            RubricDimension.MARKET_TIMING,
        ),
        thesis_verdict=ThesisFit.MISMATCH,
        thesis_evidence=True,
    )
    assert mismatch.thesis_assessment.is_supported_mismatch
    result = decide(mismatch, bundle, confident(ConfidenceLevel.LOW, 0.2))
    assert result.decision is Recommendation.PASS
    assert Guardrail.INSUFFICIENT_EVIDENCE not in result.guardrails_applied


def test_a_well_evidenced_low_score_still_passes() -> None:
    """Only *three or fewer* unassessable dimensions leaves a pass intact."""
    bundle = dossier(claims=8)
    result = decide(
        analysis(bundle, total=30, unassessable=(RubricDimension.TEAM,)),
        bundle,
        confident(ConfidenceLevel.MEDIUM, 0.5),
    )
    assert result.decision is Recommendation.PASS


# -- zero-claim and identity -------------------------------------------------


def test_a_zero_claim_dossier_becomes_watch_with_an_insufficient_evidence_rationale() -> None:
    bundle = dossier(claims=0, unknowns=4)
    result = decide(analysis(bundle, total=0), bundle, compute_confidence(None, bundle))
    assert result.decision is Recommendation.WATCH
    assert Guardrail.ZERO_CLAIM_DOSSIER in result.guardrails_applied
    assert any("no basis for either a positive or a negative call" in r for r in result.rationale)


def test_an_identity_mismatch_caps_the_recommendation_at_watch() -> None:
    bundle = dossier(claims=8)
    flagged = analysis(
        bundle, total=95, identity_warnings=("sources appear to describe another company",)
    )
    result = decide(flagged, bundle, confident())
    assert result.decision is Recommendation.WATCH
    assert Guardrail.IDENTITY_MISMATCH_CAP in result.guardrails_applied


def test_an_identity_warning_does_not_upgrade_a_pass() -> None:
    bundle = dossier(claims=8)
    flagged = analysis(bundle, total=20, identity_warnings=("possible mismatch",))
    result = decide(flagged, bundle, confident(ConfidenceLevel.MEDIUM, 0.5))
    assert result.decision is Recommendation.PASS


# -- missing website ---------------------------------------------------------


def test_a_missing_website_alone_never_forces_pass() -> None:
    """Unreadable website is an evidence gap, not a finding against the company."""
    bundle = dossier(claims=8, website_available=False)
    result = decide(analysis(bundle, total=85), bundle, confident())
    assert result.decision is Recommendation.TAKE_A_MEETING
    assert not any("website" in g for g in result.guardrails_applied)


# -- the model's suggestion --------------------------------------------------


def test_the_model_suggestion_is_recorded_but_never_obeyed() -> None:
    bundle = dossier(claims=8)
    suggested = analysis(bundle, total=20, suggested=Recommendation.TAKE_A_MEETING)
    result = decide(suggested, bundle, confident(ConfidenceLevel.MEDIUM, 0.5))
    assert result.model_suggested is Recommendation.TAKE_A_MEETING
    assert result.decision is Recommendation.PASS
    assert result.model_disagreed is True


def test_agreement_is_recorded_when_the_model_and_policy_match() -> None:
    bundle = dossier(claims=8)
    result = decide(analysis(bundle, total=70, suggested=Recommendation.WATCH), bundle, confident())
    assert result.model_disagreed is False


def test_disagreement_is_none_when_the_model_made_no_suggestion() -> None:
    bundle = dossier(claims=8)
    assert decide(analysis(bundle, total=70), bundle, confident()).model_disagreed is None


def test_the_decision_is_identical_for_every_possible_model_suggestion() -> None:
    """The suggestion is an input the policy must be blind to."""
    bundle = dossier(claims=8)
    decisions = {
        decide(analysis(bundle, total=70, suggested=s), bundle, confident()).decision
        for s in [*Recommendation, None]
    }
    assert decisions == {Recommendation.WATCH}


def test_the_rationale_reports_unassessable_points() -> None:
    bundle = dossier(claims=8)
    result = decide(
        analysis(bundle, total=20, unassessable=(RubricDimension.TEAM,)),
        bundle,
        confident(ConfidenceLevel.MEDIUM, 0.5),
    )
    assert any("could not be assessed" in line for line in result.rationale)
    assert any("of 100 points were assessable" in line for line in result.rationale)


# -- confidence --------------------------------------------------------------


def test_confidence_is_deterministic() -> None:
    bundle = dossier(claims=6)
    subject = analysis(bundle)
    assert compute_confidence(subject, bundle) == compute_confidence(subject, bundle)


def test_confidence_is_independent_of_the_score() -> None:
    """Identical coverage yields identical confidence, whatever the score."""
    bundle = dossier(claims=6)
    weak, strong = analysis(bundle, total=10), analysis(bundle, total=90)
    assert weak.total_score < strong.total_score
    assert compute_confidence(weak, bundle).score == compute_confidence(strong, bundle).score


def test_a_zero_claim_dossier_scores_zero_confidence() -> None:
    bundle = dossier(claims=0)
    result = compute_confidence(None, bundle)
    assert result.score == 0.0
    assert result.level is ConfidenceLevel.LOW
    assert "all_evidence" in result.missing
    assert any("statement about the research, not about the company" in r for r in result.reasons)


def test_broad_third_party_evidence_yields_high_confidence() -> None:
    bundle = dossier(
        claims=10,
        categories=tuple(EvidenceCategory),
        verification=VerificationStatus.THIRD_PARTY
        if hasattr(VerificationStatus, "THIRD_PARTY")
        else VerificationStatus.COMMUNITY_SIGNAL,
        unknowns=0,
        sources_cited=3,
    )
    result = compute_confidence(analysis(bundle, corroborated=3), bundle)
    assert result.score >= CONFIDENCE_HIGH_AT
    assert result.level is ConfidenceLevel.HIGH


def test_a_company_claim_heavy_dossier_scores_lower_than_a_mixed_one() -> None:
    """A company describing itself is not the same as third parties describing it."""
    shared = {
        "claims": 8,
        "categories": tuple(EvidenceCategory),
        "unknowns": 0,
        "sources_cited": 3,
    }
    own_voice = dossier(verification=VerificationStatus.COMPANY_CLAIM, **shared)
    other_voice = dossier(verification=VerificationStatus.COMMUNITY_SIGNAL, **shared)
    assert (
        compute_confidence(analysis(own_voice), own_voice).score
        < compute_confidence(analysis(other_voice), other_voice).score
    )


def test_thin_research_yields_low_confidence_and_names_what_is_missing() -> None:
    bundle = dossier(
        claims=1,
        categories=(EvidenceCategory.PRODUCT,),
        website_available=False,
        unknowns=5,
        sources_cited=1,
    )
    result = compute_confidence(analysis(bundle, total=10), bundle)
    assert result.level is ConfidenceLevel.LOW
    assert result.score < CONFIDENCE_MEDIUM_AT
    assert "company_website" in result.missing


def test_a_missing_website_reduces_confidence() -> None:
    shared = {
        "claims": 8,
        "categories": tuple(EvidenceCategory),
        "unknowns": 0,
        "sources_cited": 3,
    }
    with_site = dossier(website_available=True, **shared)
    without = dossier(website_available=False, **shared)
    assert (
        compute_confidence(analysis(without), without).score
        < compute_confidence(analysis(with_site), with_site).score
    )


def test_conflicts_reduce_confidence() -> None:
    shared = {
        "claims": 8,
        "categories": tuple(EvidenceCategory),
        "unknowns": 0,
        "sources_cited": 3,
    }
    clean, conflicted = dossier(conflicts=0, **shared), dossier(conflicts=2, **shared)
    assert (
        compute_confidence(analysis(conflicted), conflicted).score
        < compute_confidence(analysis(clean), clean).score
    )


def test_identity_warnings_reduce_confidence() -> None:
    bundle = dossier(claims=8, categories=tuple(EvidenceCategory), unknowns=0, sources_cited=3)
    subject = analysis(bundle)
    plain = compute_confidence(subject, bundle, identity_warnings=0)
    flagged = compute_confidence(subject, bundle, identity_warnings=1)
    assert flagged.score < plain.score
    assert any("identity warning" in reason for reason in flagged.reasons)


def test_unknowns_reduce_confidence_gently() -> None:
    shared = {"claims": 8, "categories": tuple(EvidenceCategory), "sources_cited": 3}
    few, many = dossier(unknowns=0, **shared), dossier(unknowns=8, **shared)
    delta = (
        compute_confidence(analysis(few), few).score
        - compute_confidence(analysis(many), many).score
    )
    assert 0 < delta <= 0.10


def test_the_independently_supported_label_earns_no_confidence_by_itself() -> None:
    """Only findings the analysis names as corroborated count."""
    bundle = dossier(claims=8, categories=tuple(EvidenceCategory), unknowns=0, sources_cited=3)
    unnamed = compute_confidence(analysis(bundle, corroborated=0), bundle)
    named = compute_confidence(analysis(bundle, corroborated=3), bundle)
    assert named.score > unnamed.score
    assert any("No finding was identified as corroborated" in r for r in unnamed.reasons)


def test_confidence_stays_within_bounds() -> None:
    for claims in (0, 1, 8, 40):
        bundle = dossier(claims=claims, unknowns=20, conflicts=5)
        result = compute_confidence(
            analysis(bundle, total=0, status=AssessmentStatus.NOT_ASSESSABLE) if claims else None,
            bundle,
            identity_warnings=3,
        )
        assert 0.0 <= result.score <= 1.0


# -- the zero-assessable rationale -------------------------------------------
#
# Regression cover for the live audit: gibsonai-com and lumro carried
# "Only 0 of 100 points were assessable" alongside a total of 14, which a memo would have
# rendered verbatim.


def all_unassessable(total: int = 14):
    """An analysis where every dimension is not_assessable, scoring ``total``."""
    bundle = dossier(claims=0, unknowns=4)
    return bundle, analysis(
        bundle, total=total, status=AssessmentStatus.NOT_ASSESSABLE, unassessable=ALL_DIMENSIONS
    )


def test_a_zero_assessable_analysis_never_claims_points_out_of_zero() -> None:
    bundle, subject = all_unassessable(total=14)
    assert subject.scored_out_of == 0
    assert subject.total_score == 14

    result = decide(subject, bundle, compute_confidence(subject, bundle))
    joined = " ".join(result.rationale)
    assert "of 0 points were assessable" not in joined
    assert "0 of 100 points" not in joined
    assert "No dimension could be assessed from the available evidence" in joined
    assert "residual uncertainty, not established merit" in joined


def test_no_rationale_line_states_a_total_above_its_own_denominator() -> None:
    """The general form of the defect, checked across a spread of analyses."""
    for total in (0, 5, 14, 24):
        bundle, subject = all_unassessable(total=total)
        result = decide(subject, bundle, compute_confidence(subject, bundle))
        for line in result.rationale:
            if match := re.search(r"Only (\d+) of 100 points were assessable", line):
                assert subject.total_score <= int(match.group(1)), line


def test_a_partially_assessable_analysis_keeps_the_existing_explanation() -> None:
    bundle = dossier(claims=8)
    subject = analysis(bundle, total=20, unassessable=(RubricDimension.TEAM,))
    assert subject.scored_out_of == 85

    result = decide(subject, bundle, confident(ConfidenceLevel.MEDIUM, 0.5))
    joined = " ".join(result.rationale)
    assert "1 dimension(s) could not be assessed" in joined
    assert "Only 85 of 100 points were assessable" in joined
    assert "residual uncertainty" not in joined


def test_a_fully_assessable_analysis_says_nothing_about_unassessable_points() -> None:
    bundle = dossier(claims=8)
    result = decide(analysis(bundle, total=50), bundle, confident())
    joined = " ".join(result.rationale)
    assert "could not be assessed" not in joined
    assert "residual uncertainty" not in joined


def test_the_wording_change_moves_no_score_band_or_guardrail() -> None:
    """The fix is presentational; nothing about the decision may shift."""
    bundle, subject = all_unassessable(total=14)
    confidence = compute_confidence(subject, bundle)
    result = decide(subject, bundle, confidence)

    assert subject.total_score == 14
    assert result.band is Recommendation.PASS
    assert result.decision is Recommendation.WATCH
    assert result.guardrails_applied == [Guardrail.ZERO_CLAIM_DOSSIER]
    assert confidence.score == 0.0
    assert confidence.level is ConfidenceLevel.LOW
