"""Domain contract tests.

Each test here pins one of the guarantees the pipeline is built on: citations resolve,
scores respect their configured maxima, totals are arithmetic rather than assertion, and
missing information stays optional instead of becoming a silent zero.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.unit import factories
from vc_scout.models.analysis import AnalysisSection, RiskItem, ScoreComponent, StartupAnalysis
from vc_scout.models.candidate import Candidate, CandidateSet
from vc_scout.models.enums import (
    ClaimLabel,
    ComponentStatus,
    Recommendation,
    RubricDimension,
    SourceKind,
    TractionKind,
)
from vc_scout.models.evidence import EvidenceClaim, EvidenceDossier
from vc_scout.models.source import SourceReference, TractionSignal, is_safe_url

# -- sources -----------------------------------------------------------------


def test_source_reference_derives_id_and_domain() -> None:
    """The stored URL stays faithful; only ``domain`` collapses ``www.`` for grouping."""
    src = SourceReference.create(
        "https://www.Acme-Ops.example/About/", kind=SourceKind.COMPANY_PAGE
    )
    assert src.source_id.startswith("src-")
    assert src.domain == "acme-ops.example"
    assert src.url == "https://www.acme-ops.example/About"


@pytest.mark.parametrize(
    "bad", ["javascript:alert(1)", "data:text/html,x", "/relative/path", "ftp://example.com"]
)
def test_source_reference_rejects_non_http_urls(bad: str) -> None:
    assert not is_safe_url(bad)
    with pytest.raises((ValidationError, ValueError)):
        SourceReference.create(bad)


def test_unobserved_source_fields_stay_none() -> None:
    src = SourceReference.create("https://acme-ops.example/")
    assert src.published_at is None
    assert src.hn_points is None
    assert src.title is None


def test_traction_signal_requires_a_source() -> None:
    with pytest.raises(ValidationError):
        TractionSignal(
            kind=TractionKind.HN_POINTS, value="212", label=ClaimLabel.THIRD_PARTY, source_ids=[]
        )


# -- candidates --------------------------------------------------------------


def test_candidate_requires_a_source_and_valid_website() -> None:
    with pytest.raises(ValidationError):
        Candidate(company_id="acme-ops", name="Acme Ops", source_ids=[])
    with pytest.raises(ValidationError):
        Candidate(
            company_id="acme-ops",
            name="Acme Ops",
            source_ids=["src-000000000001"],
            website="javascript:alert(1)",
        )


def test_candidate_ids_must_be_path_safe() -> None:
    with pytest.raises(ValidationError):
        Candidate(company_id="../escape", name="Acme", source_ids=["src-000000000001"])


def test_candidate_set_rejects_duplicate_companies() -> None:
    one = Candidate(company_id="acme-ops", name="Acme Ops", source_ids=["src-000000000001"])
    with pytest.raises(ValidationError):
        CandidateSet(run_id="r", query="q", candidates=[one, one])


# -- evidence ----------------------------------------------------------------


def test_evidence_claim_requires_source_ids() -> None:
    with pytest.raises(ValidationError):
        EvidenceClaim(
            evidence_id="ev-000000000001",
            company_id="acme-ops",
            claim="x",
            label=ClaimLabel.INFERENCE,
            source_ids=[],
        )


def test_evidence_id_must_be_content_derived() -> None:
    """An ID that does not hash to its own content is a tampered or fabricated citation."""
    good = factories.claim(factories.source())
    tampered = good.model_dump(mode="json") | {"claim": "a completely different claim"}
    with pytest.raises(ValidationError, match="does not match its content"):
        EvidenceClaim.model_validate(tampered)


def test_dossier_rejects_a_claim_citing_an_unknown_source() -> None:
    src = factories.source()
    orphan = EvidenceClaim.create(
        company_id=factories.COMPANY_ID,
        claim="Cites a source the dossier does not carry.",
        label=ClaimLabel.INFERENCE,
        source_ids=["src-ffffffffffff"],
    )
    with pytest.raises(ValidationError, match="unknown source_ids"):
        EvidenceDossier(company_id=factories.COMPANY_ID, claims=[orphan], sources=[src])


def test_dossier_accepts_resolvable_citations() -> None:
    bundle = factories.dossier()
    assert bundle.claims_for(RubricDimension.PAIN_ROI)
    assert set(bundle.claim_index()) == {bundle.claims[0].evidence_id}


def test_dossier_rejects_a_claim_about_another_company() -> None:
    src = factories.source()
    foreign = EvidenceClaim.create(
        company_id="other-co",
        claim="Belongs elsewhere.",
        label=ClaimLabel.INFERENCE,
        source_ids=[src.source_id],
    )
    with pytest.raises(ValidationError):
        EvidenceDossier(company_id=factories.COMPANY_ID, claims=[foreign], sources=[src])


# -- scoring -----------------------------------------------------------------


def test_score_component_respects_its_configured_maximum() -> None:
    with pytest.raises(ValidationError, match="configured maximum"):
        ScoreComponent.scored(RubricDimension.TRACTION, 11, evidence_ids=["ev-000000000001"])


def test_score_component_cannot_declare_a_foreign_maximum() -> None:
    with pytest.raises(ValidationError, match="rubric configures"):
        ScoreComponent(
            dimension=RubricDimension.TEAM,
            max_points=99,
            status=ComponentStatus.SCORED,
            points=1,
            evidence_ids=["ev-000000000001"],
        )


def test_unknown_component_carries_no_points_and_no_penalty_claim() -> None:
    component = ScoreComponent.unknown(RubricDimension.TEAM, rationale="no founder page found")
    assert component.points is None
    assert component.effective_points == 0
    assert component.status is ComponentStatus.UNKNOWN


def test_scored_component_must_cite_evidence() -> None:
    with pytest.raises(ValidationError, match="must cite at least one evidence_id"):
        ScoreComponent.scored(RubricDimension.TEAM, 5, evidence_ids=[])


def test_total_score_must_equal_the_component_sum() -> None:
    analysis = factories.analysis_scoring(40)
    tampered = analysis.model_dump(mode="json") | {"total_score": 95}
    with pytest.raises(ValidationError, match="does not equal the component sum"):
        StartupAnalysis.model_validate(tampered)


def test_build_fills_missing_dimensions_as_unknown() -> None:
    analysis = StartupAnalysis.build(
        company_id=factories.COMPANY_ID,
        components=[
            ScoreComponent.scored(RubricDimension.PAIN_ROI, 12, evidence_ids=["ev-000000000001"])
        ],
    )
    assert analysis.total_score == 12
    assert analysis.scored_out_of == 20
    assert len(analysis.components) == 7
    assert len(analysis.unknown_dimensions()) == 6


def test_analysis_rejects_a_missing_dimension() -> None:
    analysis = factories.analysis_scoring(20)
    truncated = analysis.model_dump(mode="json")
    truncated["components"] = truncated["components"][:3]
    truncated["total_score"] = sum(c["points"] or 0 for c in truncated["components"])
    truncated["scored_out_of"] = sum(
        c["max_points"] for c in truncated["components"] if c["status"] == "scored"
    )
    with pytest.raises(ValidationError, match="missing rubric dimensions"):
        StartupAnalysis.model_validate(truncated)


def test_suggested_recommendation_is_stored_but_optional() -> None:
    assert factories.analysis_scoring(50).suggested_recommendation is None
    suggested = factories.analysis_scoring(
        50, suggested_recommendation=Recommendation.TAKE_A_MEETING
    )
    assert suggested.suggested_recommendation is Recommendation.TAKE_A_MEETING


def test_supported_narrative_must_cite_evidence() -> None:
    with pytest.raises(ValidationError, match="must cite at least one evidence_id"):
        AnalysisSection(text="Two ex-Stripe engineers.", evidence_ids=[])
    assert AnalysisSection(text="No team information was found.", unsupported=True)


def test_supported_risk_must_cite_evidence() -> None:
    with pytest.raises(ValidationError, match="must cite at least one evidence_id"):
        RiskItem(text="Pricing undercuts incumbents.", evidence_ids=[])
    assert RiskItem(text="No pricing page was published.", unsupported=True)


def test_what_would_change_is_two_or_three_items() -> None:
    with pytest.raises(ValidationError, match="two or three items"):
        factories.analysis_scoring(50, what_would_change=["only one"])
    assert factories.analysis_scoring(50, what_would_change=["a", "b"])


# -- extra keys --------------------------------------------------------------


def test_unexpected_keys_are_rejected_rather_than_dropped() -> None:
    payload = factories.analysis_scoring(50).model_dump(mode="json")
    payload["recommendation"] = "take_a_meeting"
    with pytest.raises(ValidationError):
        StartupAnalysis.model_validate(payload)
