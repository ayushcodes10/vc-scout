"""A recorded conflict blocks the dimension it is about, not every dimension near it.

The live failure this exists for: Ticketdesk's homepage carries both a product description
and a customer-count boast, and its features page carries both again with a *different*
count. The dossier records that as a traction conflict. The validator blocked a `supported`
product wedge - twice, across two attempts - because the wedge cited product claims that
happened to live on the same two pages.

Sharing a page with a dispute is not being in dispute. These tests pin the difference.
"""

from __future__ import annotations

import pytest

from tests.unit.analysis_fixtures import analysis_payload, dossier
from vc_scout.llm.analysis_validation import (
    AnalysisValidationError,
    disputed_claim_ids,
    validate_analysis,
)
from vc_scout.models.enums import (
    EvidenceCategory,
    InferenceStatus,
    RubricDimension,
    SourceKind,
    VerificationStatus,
)
from vc_scout.models.evidence import EvidenceClaim, EvidenceConflict, SupportingExcerpt
from vc_scout.models.source import SourceReference

COMPANY = "ticketdesk-ai"

HOMEPAGE = "https://ticketdesk.example/"
FEATURES = "https://ticketdesk.example/features"
LAUNCH = "https://news.ycombinator.com/item?id=99"


def claim(
    text: str,
    *,
    category: EvidenceCategory,
    source: SourceReference,
    verification: VerificationStatus = VerificationStatus.COMPANY_CLAIM,
) -> EvidenceClaim:
    return EvidenceClaim.create(
        company_id=COMPANY,
        category=category,
        claim=text,
        excerpts=[SupportingExcerpt(source_id=source.source_id, excerpt=text[:200])],
        verification_status=verification,
        inference_status=InferenceStatus.EXPLICIT,
    )


def ticketdesk():  # type: ignore[no-untyped-def]
    """The live shape: two pages, each carrying a product fact and a disputed count."""
    home = SourceReference.create(HOMEPAGE, kind=SourceKind.COMPANY_PAGE)
    features = SourceReference.create(FEATURES, kind=SourceKind.COMPANY_PAGE)
    thread = SourceReference.create(LAUNCH, kind=SourceKind.HN_STORY)

    product_home = claim(
        "The product handles customer support tickets across chat and email, learning from "
        "the company's own documentation.",
        category=EvidenceCategory.PRODUCT,
        source=home,
    )
    product_features = claim(
        "Documented features include canned responses, multiple inboxes, tagging, API "
        "access and an embeddable widget.",
        category=EvidenceCategory.PRODUCT,
        source=features,
    )
    hundreds = claim(
        "The company states that hundreds of teams use the product.",
        category=EvidenceCategory.TRACTION,
        source=home,
    )
    thousands = claim(
        "The company states elsewhere that thousands of teams use the product.",
        category=EvidenceCategory.TRACTION,
        source=features,
    )
    launch = claim(
        "A Show HN launch post for the product drew one point and one comment.",
        category=EvidenceCategory.TRACTION,
        source=thread,
        verification=VerificationStatus.COMMUNITY_SIGNAL,
    )
    base = dossier(company_id=COMPANY, claims=1, unknowns=3, conflicts=0)
    return (
        base.model_copy(
            update={
                "claims": [product_home, product_features, hundreds, thousands, launch],
                "sources": [home, features, thread],
                "conflicts": [
                    EvidenceConflict(
                        category=EvidenceCategory.TRACTION,
                        summary=(
                            "The homepage says hundreds of teams while the features page "
                            "says thousands."
                        ),
                        source_ids=[home.source_id, features.source_id],
                    )
                ],
            }
        ),
        {
            "product_home": product_home.claim_id,
            "product_features": product_features.claim_id,
            "hundreds": hundreds.claim_id,
            "thousands": thousands.claim_id,
            "launch": launch.claim_id,
        },
    )


def rate(  # type: ignore[no-untyped-def]
    bundle,
    dimension: RubricDimension,
    *,
    status: str,
    claim_ids: list[str],
    caveats: list[str] | None = None,
    score: int = 4,
):
    payload = analysis_payload(bundle, status="partially_supported")
    for component in payload["score_components"]:
        if component["component"] == dimension.value:
            component.update(
                {
                    "assessment_status": status,
                    "score": score,
                    "rationale": "How the cited evidence supports this dimension.",
                    "evidence_claim_ids": claim_ids,
                    "caveats": caveats or [],
                }
            )
    return validate_analysis(payload, dossier=bundle)


def rejects(bundle, dimension, **kwargs) -> str:  # type: ignore[no-untyped-def]
    with pytest.raises(AnalysisValidationError) as caught:
        rate(bundle, dimension, **kwargs)
    return str(caught.value)


# -- which claims a conflict actually disputes -------------------------------


def test_only_the_claims_carrying_the_disputed_fact_are_in_dispute() -> None:
    bundle, ids = ticketdesk()
    disputed = disputed_claim_ids(bundle)

    assert disputed == {ids["hundreds"], ids["thousands"]}
    # The product descriptions sit on exactly the same two pages and are not in dispute.
    assert ids["product_home"] not in disputed
    assert ids["product_features"] not in disputed
    # Neither is a traction claim from a source the conflict does not name.
    assert ids["launch"] not in disputed


# -- the false positive this fixes -------------------------------------------


def test_a_traction_conflict_does_not_block_a_supported_wedge_on_product_claims() -> None:
    bundle, ids = ticketdesk()
    result = rate(
        bundle,
        RubricDimension.WEDGE,
        status="supported",
        claim_ids=[ids["product_home"], ids["product_features"]],
        score=11,
    )
    component = next(c for c in result.score_components if c.component is RubricDimension.WEDGE)
    assert component.assessment_status.value == "supported"
    assert component.caveats == []


@pytest.mark.parametrize(
    "dimension",
    [
        RubricDimension.WEDGE,
        RubricDimension.PAIN_ROI,
        RubricDimension.DEFENSIBILITY,
        RubricDimension.DISTRIBUTION,
        RubricDimension.TEAM,
        RubricDimension.MARKET_TIMING,
    ],
)
def test_one_page_carrying_both_facts_does_not_contaminate_every_dimension(
    dimension: RubricDimension,
) -> None:
    """A shared source is not shared evidence. Nothing but traction is in dispute here."""
    bundle, ids = ticketdesk()
    result = rate(
        bundle,
        dimension,
        status="supported",
        claim_ids=[ids["product_home"]],
        score=3,
    )
    assert (
        next(c for c in result.score_components if c.component is dimension).assessment_status.value
        == "supported"
    )


# -- what still blocks -------------------------------------------------------


def test_a_traction_conflict_blocks_a_supported_traction_rating(  # noqa: D401
) -> None:
    bundle, ids = ticketdesk()
    message = rejects(
        bundle,
        RubricDimension.TRACTION,
        status="supported",
        claim_ids=[ids["hundreds"], ids["thousands"]],
        score=6,
    )
    assert "conflict" in message
    assert ids["hundreds"] in message
    assert "contradicted" in message


def test_it_blocks_supported_traction_even_when_the_dispute_is_not_cited() -> None:
    """The conflict is about traction, which is what this dimension assesses."""
    bundle, ids = ticketdesk()
    message = rejects(
        bundle,
        RubricDimension.TRACTION,
        status="supported",
        claim_ids=[ids["launch"]],
        score=5,
    )
    assert "conflict about traction" in message


def test_a_caveat_is_what_makes_a_conflicted_traction_rating_acceptable() -> None:
    bundle, ids = ticketdesk()
    result = rate(
        bundle,
        RubricDimension.TRACTION,
        status="supported",
        claim_ids=[ids["hundreds"], ids["thousands"]],
        caveats=["The homepage and the features page give different customer counts."],
        score=6,
    )
    component = next(c for c in result.score_components if c.component is RubricDimension.TRACTION)
    assert component.assessment_status.value == "supported"
    assert component.caveats


def test_contradicted_remains_the_other_honest_answer() -> None:
    bundle, ids = ticketdesk()
    result = rate(
        bundle,
        RubricDimension.TRACTION,
        status="contradicted",
        claim_ids=[ids["hundreds"], ids["thousands"]],
        score=6,
    )
    assert (
        next(
            c for c in result.score_components if c.component is RubricDimension.TRACTION
        ).assessment_status.value
        == "contradicted"
    )


def test_a_cross_category_dimension_is_blocked_when_it_cites_the_dispute_itself() -> None:
    """Distribution is not what a traction conflict is about - until it cites the dispute."""
    bundle, ids = ticketdesk()
    clean = rate(
        bundle,
        RubricDimension.DISTRIBUTION,
        status="supported",
        claim_ids=[ids["product_features"]],
        score=8,
    )
    assert (
        next(
            c for c in clean.score_components if c.component is RubricDimension.DISTRIBUTION
        ).assessment_status.value
        == "supported"
    )

    message = rejects(
        bundle,
        RubricDimension.DISTRIBUTION,
        status="supported",
        claim_ids=[ids["product_features"], ids["thousands"]],
        score=8,
    )
    assert ids["thousands"] in message


def test_a_dossier_with_no_conflict_blocks_nothing(  # noqa: D401
) -> None:
    bundle, ids = ticketdesk()
    unconflicted = bundle.model_copy(update={"conflicts": []})
    result = rate(
        unconflicted,
        RubricDimension.TRACTION,
        status="supported",
        claim_ids=[ids["hundreds"]],
        score=6,
    )
    assert (
        next(
            c for c in result.score_components if c.component is RubricDimension.TRACTION
        ).assessment_status.value
        == "supported"
    )


# -- nothing else was loosened ------------------------------------------------


def test_the_result_claim_cap_still_applies_over_a_conflicted_dossier() -> None:
    """The conflict change must not become a way past the self-reported-result cap."""
    bundle, ids = ticketdesk()
    # A caveated rating whose rationale carries no figure is accepted.
    accepted = rate(
        bundle,
        RubricDimension.TRACTION,
        status="supported",
        claim_ids=[ids["hundreds"]],
        caveats=["Counts conflict across pages."],
        score=6,
    )
    assert (
        next(
            c for c in accepted.score_components if c.component is RubricDimension.TRACTION
        ).assessment_status.value
        == "supported"
    )

    # A rationale that asserts a self-reported result is still refused, caveat or not.
    payload = analysis_payload(bundle, status="partially_supported")
    for component in payload["score_components"]:
        if component["component"] == RubricDimension.TRACTION.value:
            component.update(
                {
                    "assessment_status": "supported",
                    "score": 6,
                    "rationale": "The company reports 1,200 paying customers.",
                    "evidence_claim_ids": [ids["hundreds"]],
                    "caveats": ["Counts conflict across pages."],
                }
            )
    with pytest.raises(AnalysisValidationError, match="customer or user count"):
        validate_analysis(payload, dossier=bundle)


def test_an_unknown_evidence_id_is_still_rejected() -> None:
    bundle, ids = ticketdesk()
    message = rejects(
        bundle,
        RubricDimension.WEDGE,
        status="supported",
        claim_ids=[ids["product_home"], "ev-ffffffffffff"],
        score=11,
    )
    assert "ev-ffffffffffff" in message
