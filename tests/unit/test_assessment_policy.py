"""The source-to-assessment policy, as a deterministic evaluation matrix.

The audited live run graded zero of 105 component slots `supported`, which capped every
candidate's achievable total at 63 and made the take-a-meeting band unreachable before any
judgement about the companies. The cause was reading "company-authored" as "cannot support
anything".

The correction is not "score higher". It is a stated line between what a company source can
establish - what the product *is* - and what it cannot establish alone: a result, an
advantage, a scale. This file is that line, written as cases.
"""

from __future__ import annotations

import pytest

from tests.unit.analysis_fixtures import analysis_payload, component_payload, dossier
from vc_scout.assessment_policy import (
    ASSESSMENT_POLICY,
    ASSESSMENT_POLICY_VERSION,
    GUARDED_QUANTITATIVE,
    quantitative_outcome_terms,
    render_policy,
)
from vc_scout.llm.analysis_validation import AnalysisValidationError, validate_analysis
from vc_scout.models.enums import (
    EvidenceCategory,
    InferenceStatus,
    RubricDimension,
    VerificationStatus,
)
from vc_scout.models.evidence import EvidenceClaim, EvidenceConflict, SupportingExcerpt

COMPANY = "acme-ops"


def _prompt_text() -> str:
    from vc_scout.prompts import prompt_text
    from vc_scout.stages.analysis import ANALYSIS_PROMPT_VERSION

    return prompt_text(ANALYSIS_PROMPT_VERSION)


def bundle_with(
    claim_text: str,
    *,
    verification: VerificationStatus = VerificationStatus.COMPANY_CLAIM,
    conflicted: bool = False,
):  # type: ignore[no-untyped-def]
    """A dossier whose first claim is exactly the one a case is about."""
    base = dossier(claims=1, conflicts=0, unknowns=3)
    home = base.sources[0]
    other = base.sources[1]
    claim = EvidenceClaim.create(
        company_id=COMPANY,
        category=EvidenceCategory.PRODUCT,
        claim=claim_text,
        excerpts=[
            SupportingExcerpt(source_id=home.source_id, excerpt=claim_text[:200]),
            *(
                [SupportingExcerpt(source_id=other.source_id, excerpt=claim_text[:200])]
                if verification is VerificationStatus.INDEPENDENTLY_SUPPORTED
                else []
            ),
        ],
        verification_status=verification,
        inference_status=InferenceStatus.EXPLICIT,
    )
    conflicts = (
        [
            EvidenceConflict(
                category=EvidenceCategory.TRACTION,
                summary="The homepage says hundreds of teams; the features page says thousands.",
                source_ids=[home.source_id, other.source_id],
            )
        ]
        if conflicted
        else []
    )
    return base.model_copy(update={"claims": [claim], "conflicts": conflicts})


def rate(  # type: ignore[no-untyped-def]
    bundle,
    dimension: RubricDimension,
    *,
    status: str,
    rationale: str,
    caveats: list[str] | None = None,
    score: int = 3,
):
    """Validate one payload whose named dimension carries the rating under test."""
    claim_id = bundle.claims[0].claim_id if bundle.claims else None
    payload = analysis_payload(bundle, status="partially_supported")
    for component in payload["score_components"]:
        if component["component"] == dimension.value:
            component.update(
                {
                    "assessment_status": status,
                    "rationale": rationale,
                    "score": score,
                    "caveats": caveats or [],
                    "evidence_claim_ids": [claim_id] if claim_id else [],
                }
            )
    return validate_analysis(payload, dossier=bundle)


def rejects(  # type: ignore[no-untyped-def]
    bundle, dimension: RubricDimension, **kwargs
) -> str:
    with pytest.raises(AnalysisValidationError) as caught:
        rate(bundle, dimension, **kwargs)
    return str(caught.value)


# -- the table itself --------------------------------------------------------


def test_the_policy_covers_every_dimension_once_and_is_versioned() -> None:
    assert ASSESSMENT_POLICY_VERSION
    assert [item.key for item in ASSESSMENT_POLICY] == list(RubricDimension)
    for item in ASSESSMENT_POLICY:
        assert item.supportable and item.capped


def test_the_policy_reaches_the_model_through_the_rendered_payload() -> None:
    """One definition. A prompt file restating the table could drift from the validator."""
    from tests.unit.analysis_fixtures import dossier as make
    from vc_scout.models.candidate import Candidate
    from vc_scout.stages.analysis import render_dossier_payload

    bundle = make(claims=2)
    candidate = Candidate(company_id=COMPANY, name="Acme Ops", source_ids=["src-000000000000"])
    payload = render_dossier_payload(candidate, bundle)

    assert ASSESSMENT_POLICY_VERSION in payload
    for line in render_policy():
        assert line.strip() in payload
    for dimension in RubricDimension:
        assert f"- {dimension.value}:" in payload


# -- the evaluation matrix ---------------------------------------------------


def test_an_explicit_company_product_description_may_support_the_wedge() -> None:
    """The change this whole calibration exists for: a concrete fact is evidence."""
    bundle = bundle_with(
        "The product documents a Jira integration and publishes a per-seat monthly price."
    )
    result = rate(
        bundle,
        RubricDimension.WEDGE,
        status="supported",
        rationale=(
            "The company documents a Jira integration and a published per-seat price, which "
            "places the wedge inside an existing system of record."
        ),
        score=12,
    )
    component = next(c for c in result.score_components if c.component is RubricDimension.WEDGE)
    assert component.assessment_status.value == "supported"
    assert component.score == 12


def test_marketing_adjectives_are_not_a_concrete_fact() -> None:
    """Not mechanically rejectable, so the policy states it and the prompt shows it.

    A keyword checker for "revolutionary" would be exactly the false-confidence hack this
    change is not allowed to introduce - it would pass a rationale that simply avoided the
    word. What is enforced is that the table says it and the model is given the table.
    """
    wedge = next(item for item in ASSESSMENT_POLICY if item.key is RubricDimension.WEDGE)
    assert "marketing adjectives" in wedge.capped.lower()
    assert "revolutionary" in wedge.capped.lower()


def test_a_self_reported_saving_may_not_be_supported() -> None:
    bundle = bundle_with("The company states the tool saves 80% of manual triage time.")
    message = rejects(
        bundle,
        RubricDimension.PAIN_ROI,
        status="supported",
        rationale="The company reports the tool saves 80% of manual triage time.",
        score=15,
    )
    assert "may not be supported" in message
    assert "percentage improvement" in message
    assert "partially_supported" in message


def test_the_same_saving_is_accepted_as_partially_supported() -> None:
    bundle = bundle_with("The company states the tool saves 80% of manual triage time.")
    result = rate(
        bundle,
        RubricDimension.PAIN_ROI,
        status="partially_supported",
        rationale="The company reports the tool saves 80% of manual triage time.",
        score=12,
    )
    component = next(c for c in result.score_components if c.component is RubricDimension.PAIN_ROI)
    assert component.assessment_status.value == "partially_supported"


def test_a_hacker_news_launch_record_may_support_freshness() -> None:
    """A third party recorded it, and freshness is exactly what it shows."""
    bundle = bundle_with(
        "The Show HN thread is dated three weeks ago and carries 42 points and 13 comments.",
        verification=VerificationStatus.COMMUNITY_SIGNAL,
    )
    result = rate(
        bundle,
        RubricDimension.TRACTION,
        status="supported",
        rationale=(
            "The Show HN launch is three weeks old with 42 points and 13 comments, which "
            "establishes recency and a measurable level of community attention."
        ),
        score=6,
    )
    component = next(c for c in result.score_components if c.component is RubricDimension.TRACTION)
    assert component.assessment_status.value == "supported"


def test_a_self_reported_customer_count_may_not_be_supported() -> None:
    bundle = bundle_with("The company reports 1,200 paying customers.")
    message = rejects(
        bundle,
        RubricDimension.TRACTION,
        status="supported",
        rationale="The company reports 1,200 paying customers using the product.",
        score=8,
    )
    assert "customer or user count" in message


def test_a_self_reported_revenue_figure_may_not_be_supported() -> None:
    bundle = bundle_with("The founder posted that the company reached $2.4M ARR.")
    message = rejects(
        bundle,
        RubricDimension.TRACTION,
        status="supported",
        rationale="The founder states the company reached $2.4M ARR this year.",
        score=8,
    )
    assert "revenue or retention metric" in message


def test_a_named_founder_biography_may_support_factual_team_composition() -> None:
    bundle = bundle_with(
        "The about page names Lawrance Nyakiso as founder, with 11 years as a security "
        "architect in banking."
    )
    result = rate(
        bundle,
        RubricDimension.TEAM,
        status="supported",
        rationale=(
            "The about page names the sole founder and an eleven-year security-architecture "
            "background in regulated industries."
        ),
        score=9,
    )
    component = next(c for c in result.score_components if c.component is RubricDimension.TEAM)
    assert component.assessment_status.value == "supported"


def test_an_exceptional_team_judgement_is_not_the_same_evidence() -> None:
    team = next(item for item in ASSESSMENT_POLICY if item.key is RubricDimension.TEAM)
    assert "quality judgement" in team.capped.lower()
    assert "exits" in team.capped.lower()
    # Team is not in the quantitative guard: a biography carries dates and years, and
    # flagging those would block the very statement the policy allows.
    assert RubricDimension.TEAM not in GUARDED_QUANTITATIVE


def test_an_integration_is_a_fact_and_a_moat_is_a_claim() -> None:
    bundle = bundle_with("The product integrates with Jira, Linear and GitHub.")
    supported = rate(
        bundle,
        RubricDimension.DEFENSIBILITY,
        status="supported",
        rationale="Three named issue-tracker integrations are documented on the site.",
        score=9,
    )
    assert (
        next(
            c for c in supported.score_components if c.component is RubricDimension.DEFENSIBILITY
        ).assessment_status.value
        == "supported"
    )

    defensibility = next(
        item for item in ASSESSMENT_POLICY if item.key is RubricDimension.DEFENSIBILITY
    )
    assert "moat" in defensibility.capped.lower()


def test_the_independently_supported_label_earns_no_status_credit() -> None:
    """Relabelling the provenance must not change a single validated outcome.

    The label is mechanical - it means "cites two sources" - and the first live run
    produced one that was two sources supporting different halves of a compound statement.
    Nothing in the validator reads it to grant a status; corroboration has to be argued in
    the rationale and recorded under `corroborated`.
    """
    company = bundle_with("The company reports 1,200 paying customers.")
    independent = bundle_with(
        "The company reports 1,200 paying customers.",
        verification=VerificationStatus.INDEPENDENTLY_SUPPORTED,
    )
    kwargs = {
        "status": "partially_supported",
        "rationale": "Reported customer scale, taken as stated.",
        "score": 5,
    }
    first = rate(company, RubricDimension.TRACTION, **kwargs)  # type: ignore[arg-type]
    second = rate(independent, RubricDimension.TRACTION, **kwargs)  # type: ignore[arg-type]

    def status_map(result):  # type: ignore[no-untyped-def]
        return {c.component: (c.assessment_status, c.score) for c in result.score_components}

    assert status_map(first) == status_map(second)
    assert "grants nothing" in _prompt_text().lower() or "alone grants nothing" in _prompt_text()


def test_corroboration_is_what_lifts_the_quantitative_cap_not_the_label_alone() -> None:
    """The cap keys on "only the company said it", which is the documented line.

    A claim carrying a second, separate voice is no longer company-authored-only, so the
    mechanical guard steps aside - and from there it is the model's argument, checked by a
    reader, that has to carry the rating. That is the boundary of what a validator can
    honestly decide.
    """
    result = rate(
        bundle_with(
            "Two separate write-ups report 1,200 paying customers.",
            verification=VerificationStatus.INDEPENDENTLY_SUPPORTED,
        ),
        RubricDimension.TRACTION,
        status="supported",
        rationale="Two separate sources report the same customer count of 1,200.",
        score=8,
    )
    assert (
        next(
            c for c in result.score_components if c.component is RubricDimension.TRACTION
        ).assessment_status.value
        == "supported"
    )

    # The identical rationale on the company's word alone is refused.
    assert "customer or user count" in rejects(
        bundle_with("The company reports 1,200 paying customers."),
        RubricDimension.TRACTION,
        status="supported",
        rationale="Two separate sources report the same customer count of 1,200.",
        score=8,
    )


def test_a_rating_over_a_recorded_conflict_may_not_be_supported_silently() -> None:
    bundle = bundle_with("The homepage says hundreds of teams use the product.", conflicted=True)
    message = rejects(
        bundle,
        RubricDimension.DISTRIBUTION,
        status="supported",
        rationale="Adoption across many teams is documented on the company's own pages.",
        score=10,
    )
    assert "records a conflict" in message
    assert "contradicted" in message


def test_the_same_conflicted_rating_is_accepted_once_it_is_caveated() -> None:
    bundle = bundle_with("The homepage says hundreds of teams use the product.", conflicted=True)
    result = rate(
        bundle,
        RubricDimension.DISTRIBUTION,
        status="supported",
        rationale="Self-serve availability is documented on the company's own pages.",
        caveats=["The dossier records a conflict about customer scale across two pages."],
        score=10,
    )
    component = next(
        c for c in result.score_components if c.component is RubricDimension.DISTRIBUTION
    )
    assert component.caveats


def test_no_evidence_stays_not_assessable_in_neutral_language() -> None:
    bundle = dossier(claims=0, unknowns=3)
    payload = analysis_payload(bundle)
    result = validate_analysis(payload, dossier=bundle)

    assert {c.assessment_status.value for c in result.score_components} == {"not_assessable"}
    assert all(not c.evidence_claim_ids for c in result.score_components)
    assert all(c.unknown_references for c in result.score_components)


# -- the detector itself -----------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Saves 80% of triage time.",
        "Cuts onboarding time by 40%.",
        "Reached $2.4M ARR.",
        "Retention is 95% after a year.",
        "Hundreds of teams use it.",
        "Reports 1,200 active users.",
        "A $40 billion market for compliance tooling.",
    ],
)
def test_the_detector_finds_a_result_claim(text: str) -> None:
    assert quantitative_outcome_terms(text)


@pytest.mark.parametrize(
    "text",
    [
        "Decomposes GDPR into 191 structured requirements across 7 modules.",
        "The Show HN thread has 42 points and 13 comments.",
        "Integrates with Jira, Linear and GitHub.",
        "Pricing starts at $29 per seat per month.",
        "Founded by a security architect with 11 years in banking.",
        "Launched on 12 March 2026.",
        "The EU AI Act took effect in 2026, which is the stated reason this is buildable now.",
    ],
)
def test_the_detector_leaves_a_concrete_fact_alone(text: str) -> None:
    """A checker that fired on any digit would recreate the defect it is here to fix."""
    assert not quantitative_outcome_terms(text)


def test_a_component_payload_helper_still_matches_the_contract() -> None:
    payload = component_payload(RubricDimension.WEDGE, score=4, status="supported")
    assert set(payload) == {
        "component",
        "score",
        "assessment_status",
        "rationale",
        "evidence_claim_ids",
        "unknown_references",
        "caveats",
    }
