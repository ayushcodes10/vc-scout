"""Validation of model-supplied analysis against the dossier it was given.

Each test here describes something a model plausibly does - cites a claim it was never
shown, scores a dimension above what its own assessment permits, quotes a market size out
of thin air - and pins the rejection.
"""

from __future__ import annotations

import pytest

from tests.unit.analysis_fixtures import (
    analysis_payload,
    component_payload,
    dossier,
    unknown_ref,
)
from vc_scout.llm.analysis_validation import (
    AnalysisValidationError,
    find_unsupported_market_numbers,
    index_dossier,
    validate_analysis,
)
from vc_scout.models.enums import (
    AssessmentStatus,
    LlmErrorCategory,
    Recommendation,
    RubricDimension,
    ThesisFit,
)
from vc_scout.rubric import RUBRIC


def bundle():
    return dossier(claims=4, unknowns=2)


def validate(payload, subject=None):
    return validate_analysis(payload, dossier=subject or bundle())


def rejects(payload, subject=None) -> AnalysisValidationError:
    with pytest.raises(AnalysisValidationError) as caught:
        validate(payload, subject)
    return caught.value


# -- a valid analysis --------------------------------------------------------


def test_a_well_formed_analysis_validates() -> None:
    subject = bundle()
    result = validate(analysis_payload(subject), subject)

    assert len(result.score_components) == len(RUBRIC) == 7
    assert result.total_score == sum(c.score for c in result.score_components)
    assert result.model_suggested_recommendation is Recommendation.WATCH
    assert result.thesis_assessment.verdict is ThesisFit.ALIGNED
    assert len(result.recommendation_changers) == 2


def test_every_component_carries_its_configured_maximum() -> None:
    subject = bundle()
    result = validate(analysis_payload(subject), subject)
    assert {c.component: c.maximum for c in result.score_components} == {
        spec.key: spec.max_points for spec in RUBRIC
    }
    assert sum(c.maximum for c in result.score_components) == 100


def test_the_total_is_recomputed_from_the_components() -> None:
    subject = bundle()
    scores = {RubricDimension.PAIN_ROI: 12, RubricDimension.WEDGE: 9}
    result = validate(analysis_payload(subject, scores=scores), subject)
    assert result.total_score == 12 + 9 + 5 * 5


@pytest.mark.parametrize(
    "status", ["supported", "partially_supported", "contradicted", "not_assessable"]
)
def test_every_assessment_status_is_accepted(status: str) -> None:
    subject = bundle()
    payload = analysis_payload(subject, status=status, scores=dict.fromkeys(RubricDimension, 3))
    result = validate(payload, subject)
    assert {c.assessment_status.value for c in result.score_components} == {status}


# -- score ceilings ----------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "score", "allowed"),
    [
        ("supported", 20, True),
        ("partially_supported", 14, True),
        ("partially_supported", 15, False),
        ("not_assessable", 10, True),
        ("not_assessable", 11, False),
        ("contradicted", 20, True),
    ],
)
def test_status_ceilings_are_enforced(status: str, score: int, allowed: bool) -> None:
    subject = bundle()
    claim = subject.claims[0].claim_id
    payload = analysis_payload(subject)
    payload["score_components"][0] = component_payload(
        RubricDimension.PAIN_ROI,
        score=score,
        status=status,
        claim_id=claim,
        unknown=unknown_ref(subject) if status == "not_assessable" else None,
    )
    if allowed:
        assert validate(payload, subject).score_components[0].score == score
    else:
        error = rejects(payload, subject)
        assert error.category is LlmErrorCategory.INVALID_SCORE
        assert "may score at most" in error.errors[0]


def test_a_score_above_the_dimension_maximum_is_rejected() -> None:
    subject = bundle()
    payload = analysis_payload(subject)
    payload["score_components"][0] = component_payload(
        RubricDimension.PAIN_ROI, score=25, claim_id=subject.claims[0].claim_id
    )
    error = rejects(payload, subject)
    assert error.category is LlmErrorCategory.INVALID_SCORE
    assert "outside 0-20" in error.errors[0]


def test_not_assessable_is_neither_forced_to_zero_nor_to_the_midpoint() -> None:
    """The model must choose, and the validator must let it choose anything up to the cap."""
    subject = bundle()
    for score in (0, 3, 7, 10):
        payload = analysis_payload(subject)
        payload["score_components"][0] = component_payload(
            RubricDimension.PAIN_ROI,
            score=score,
            status="not_assessable",
            unknown=unknown_ref(subject),
        )
        assert validate(payload, subject).score_components[0].score == score


# -- structural rules --------------------------------------------------------


def test_a_missing_rubric_dimension_is_rejected() -> None:
    subject = bundle()
    payload = analysis_payload(subject)
    payload["score_components"] = payload["score_components"][:5]
    error = rejects(payload, subject)
    assert "missing rubric dimension" in " ".join(error.errors)


def test_a_duplicated_rubric_dimension_is_rejected() -> None:
    subject = bundle()
    payload = analysis_payload(subject)
    payload["score_components"].append(dict(payload["score_components"][0]))
    error = rejects(payload, subject)
    assert "appears more than once" in " ".join(error.errors)


def test_an_invalid_dimension_name_is_rejected() -> None:
    subject = bundle()
    payload = analysis_payload(subject)
    payload["score_components"][0]["component"] = "vibes"
    assert "not a rubric dimension" in " ".join(rejects(payload, subject).errors)


def test_an_invalid_assessment_status_is_rejected() -> None:
    subject = bundle()
    payload = analysis_payload(subject)
    payload["score_components"][0]["assessment_status"] = "looks_fine"
    assert "assessment_status" in " ".join(rejects(payload, subject).errors)


def test_an_invalid_model_recommendation_is_rejected() -> None:
    subject = bundle()
    payload = analysis_payload(subject, model_suggested_recommendation="invest_now")
    error = rejects(payload, subject)
    assert error.category is LlmErrorCategory.INVALID_RECOMMENDATION


# -- reference integrity -----------------------------------------------------


def test_an_unknown_evidence_claim_id_is_rejected() -> None:
    subject = bundle()
    payload = analysis_payload(subject)
    payload["score_components"][0]["evidence_claim_ids"] = ["ev-ffffffffffff"]
    error = rejects(payload, subject)
    assert error.category is LlmErrorCategory.UNKNOWN_EVIDENCE_REFERENCE
    assert "do not exist in this candidate's dossier" in error.errors[0]
    # The retry is told which values are valid.
    assert subject.claims[0].claim_id in error.errors[0]


def test_an_unknown_unknown_reference_is_rejected() -> None:
    subject = bundle()
    payload = analysis_payload(subject)
    payload["score_components"][0]["unknown_references"] = ["unk-ffffffffffff"]
    error = rejects(payload, subject)
    assert error.category is LlmErrorCategory.UNKNOWN_EVIDENCE_REFERENCE
    assert "unknown reference" in error.errors[0]


def test_a_valid_unknown_reference_resolves() -> None:
    subject = bundle()
    payload = analysis_payload(subject)
    payload["score_components"][0] = component_payload(
        RubricDimension.PAIN_ROI, score=2, status="not_assessable", unknown=unknown_ref(subject)
    )
    result = validate(payload, subject)
    assert result.score_components[0].unknown_references == [unknown_ref(subject)]


def test_duplicate_references_are_normalised_deterministically() -> None:
    subject = bundle()
    claim = subject.claims[0].claim_id
    payload = analysis_payload(subject)
    payload["score_components"][0]["evidence_claim_ids"] = [claim, claim, f" {claim} "]
    assert validate(payload, subject).score_components[0].evidence_claim_ids == [claim]


def test_a_risk_must_cite_evidence_or_an_unknown() -> None:
    subject = bundle()
    payload = analysis_payload(
        subject,
        extra_sections=[
            {"kind": "risk", "text": "A worry.", "evidence_claim_ids": [], "unknown_references": []}
        ],
    )
    assert "must cite evidence claim IDs" in " ".join(rejects(payload, subject).errors)


def test_a_risk_arising_from_an_unknown_is_accepted() -> None:
    subject = bundle()
    payload = analysis_payload(
        subject,
        extra_sections=[
            {
                "kind": "risk",
                "text": "No pricing was published.",
                "evidence_claim_ids": [],
                "unknown_references": [unknown_ref(subject)],
            }
        ],
    )
    assert validate(payload, subject).risks[0].unknown_references == [unknown_ref(subject)]


def test_a_competitive_observation_without_evidence_is_rejected() -> None:
    subject = bundle()
    payload = analysis_payload(
        subject,
        extra_sections=[
            {
                "kind": "competitor",
                "text": "Competes with Zendesk.",
                "evidence_claim_ids": [],
                "unknown_references": [],
            }
        ],
    )
    assert "may only be named when a supplied claim names it" in " ".join(
        rejects(payload, subject).errors
    )


# -- market-size figures -----------------------------------------------------


@pytest.mark.parametrize(
    "sentence",
    [
        "The TAM is $40 billion for this category.",
        "This addresses a 12 million seat market opportunity.",
        "The market size is estimated at $3.4bn.",
        "They target a $500 million industry.",
    ],
)
def test_unsupported_market_size_figures_are_caught(sentence: str) -> None:
    assert find_unsupported_market_numbers(sentence, corpus="nothing relevant here")


def test_a_figure_the_evidence_carries_is_allowed() -> None:
    corpus = index_dossier(bundle()).corpus + " the company reports $49 per month pricing"
    assert not find_unsupported_market_numbers("Pricing starts at $49 per month.", corpus)


def test_an_invented_market_size_rejects_the_analysis() -> None:
    subject = bundle()
    payload = analysis_payload(
        subject, plain_language_product="An agent addressing a $40 billion TAM."
    )
    assert "unsupported market-size figures" in " ".join(rejects(payload, subject).errors)


def test_ordinary_prose_without_figures_is_untouched() -> None:
    assert find_unsupported_market_numbers("They sell to small plumbing firms.", "") == []


# -- recommendation changers -------------------------------------------------


@pytest.mark.parametrize("count", [0, 1, 4, 5])
def test_the_wrong_number_of_recommendation_changers_is_rejected(count: int) -> None:
    subject = bundle()
    error = rejects(analysis_payload(subject, changers=count), subject)
    assert "exactly 2 or 3 items are required" in " ".join(error.errors)


@pytest.mark.parametrize("count", [2, 3])
def test_two_or_three_recommendation_changers_are_accepted(count: int) -> None:
    subject = bundle()
    assert (
        len(validate(analysis_payload(subject, changers=count), subject).recommendation_changers)
        == count
    )


# -- error collection --------------------------------------------------------


def test_every_problem_is_reported_at_once_for_the_retry() -> None:
    subject = bundle()
    payload = analysis_payload(subject, changers=0)
    payload["score_components"][0]["evidence_claim_ids"] = ["ev-ffffffffffff"]
    payload["score_components"][1]["score"] = 99
    assert len(rejects(payload, subject).errors) >= 3


def test_a_zero_claim_dossier_can_still_produce_a_valid_analysis() -> None:
    """Every dimension is not_assessable, anchored on recorded unknowns."""
    subject = dossier(claims=0, unknowns=3)
    result = validate(analysis_payload(subject), subject)
    assert result.total_score == 0
    assert all(
        c.assessment_status is AssessmentStatus.NOT_ASSESSABLE for c in result.score_components
    )
    assert result.thesis_assessment.verdict is ThesisFit.UNDETERMINED
