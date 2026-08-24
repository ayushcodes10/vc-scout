"""Domain contract tests.

Each test here pins one of the guarantees the pipeline is built on: citations resolve,
scores respect their configured maxima, totals are arithmetic rather than assertion, and
missing information stays optional instead of becoming a silent zero.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from tests.unit import analysis_fixtures, factories
from vc_scout.models.analysis import (
    AnalysisSection,
    CompetitiveObservation,
    RiskItem,
    ScoreComponent,
    StartupAnalysis,
    ThesisAssessment,
    ceiling_for,
)
from vc_scout.models.candidate import Candidate, CandidateSet
from vc_scout.models.enums import (
    AssessmentStatus,
    ClaimLabel,
    EvidenceCategory,
    InferenceStatus,
    Recommendation,
    RubricDimension,
    SourceKind,
    ThesisFit,
    TractionKind,
    VerificationStatus,
)
from vc_scout.models.evidence import (
    EvidenceClaim,
    EvidenceConflict,
    EvidenceDossier,
    EvidenceUnknown,
    SupportingExcerpt,
)
from vc_scout.models.source import SourceReference, TractionSignal, is_safe_url
from vc_scout.rubric import max_points_for

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


def test_evidence_claim_requires_a_supporting_excerpt() -> None:
    src = factories.source()
    with pytest.raises(ValidationError):
        EvidenceClaim(
            claim_id="ev-000000000001",
            company_id="acme-ops",
            category=EvidenceCategory.PRODUCT,
            claim="x",
            source_ids=[src.source_id],
            excerpts=[],
            verification_status=VerificationStatus.COMPANY_CLAIM,
            inference_status=InferenceStatus.EXPLICIT,
        )


def test_claim_id_must_be_content_derived() -> None:
    """An ID that does not hash to its own content is a tampered or fabricated citation."""
    good = factories.claim(factories.source())
    tampered = good.model_dump(mode="json") | {"claim": "a completely different claim"}
    with pytest.raises(ValidationError, match="does not match its content"):
        EvidenceClaim.model_validate(tampered)


def test_every_cited_source_needs_its_own_excerpt() -> None:
    src, other = factories.source(), factories.source("https://acme-ops.example/pricing")
    good = factories.claim(src)
    payload = good.model_dump(mode="json")
    payload["source_ids"] = [src.source_id, other.source_id]
    with pytest.raises(ValidationError, match="no supporting excerpt"):
        EvidenceClaim.model_validate(payload)


def test_an_excerpt_may_not_cite_a_source_the_claim_does_not_list() -> None:
    src, other = factories.source(), factories.source("https://acme-ops.example/pricing")
    payload = factories.claim(src).model_dump(mode="json")
    payload["excerpts"].append({"source_id": other.source_id, "excerpt": "some other text"})
    with pytest.raises(ValidationError, match="does not list"):
        EvidenceClaim.model_validate(payload)


def test_independently_supported_requires_two_separate_sources() -> None:
    """The label a model is most tempted to over-apply is the one that is checked."""
    src = factories.source()
    with pytest.raises(ValidationError, match="at least two separate sources"):
        factories.claim(src, verification=VerificationStatus.INDEPENDENTLY_SUPPORTED)


def test_two_sources_do_support_an_independently_supported_claim() -> None:
    src, other = factories.source(), factories.source("https://news.example/write-up")
    assert EvidenceClaim.create(
        company_id=factories.COMPANY_ID,
        category=EvidenceCategory.TRACTION,
        claim="Two separate sources describe the same customer.",
        excerpts=[
            SupportingExcerpt(source_id=src.source_id, excerpt="a named customer"),
            SupportingExcerpt(source_id=other.source_id, excerpt="the same named customer"),
        ],
        verification_status=VerificationStatus.INDEPENDENTLY_SUPPORTED,
        inference_status=InferenceStatus.EXPLICIT,
    )


def test_dossier_rejects_a_claim_citing_an_unknown_source() -> None:
    src = factories.source()
    orphan = EvidenceClaim.create(
        company_id=factories.COMPANY_ID,
        category=EvidenceCategory.RISK,
        claim="Cites a source the dossier does not carry.",
        excerpts=[SupportingExcerpt(source_id="src-ffffffffffff", excerpt="not carried here")],
        verification_status=VerificationStatus.COMPANY_CLAIM,
        inference_status=InferenceStatus.INFERRED,
    )
    with pytest.raises(ValidationError, match="unknown source_ids"):
        EvidenceDossier(company_id=factories.COMPANY_ID, claims=[orphan], sources=[src])


def test_dossier_accepts_resolvable_citations() -> None:
    bundle = factories.dossier()
    assert bundle.claims_for(EvidenceCategory.PRODUCT)
    assert set(bundle.claim_index()) == {bundle.claims[0].claim_id}


def test_dossier_rejects_a_claim_about_another_company() -> None:
    src = factories.source()
    foreign = EvidenceClaim.create(
        company_id="other-co",
        category=EvidenceCategory.TEAM,
        claim="Belongs elsewhere.",
        excerpts=[SupportingExcerpt(source_id=src.source_id, excerpt="belongs elsewhere")],
        verification_status=VerificationStatus.COMPANY_CLAIM,
        inference_status=InferenceStatus.EXPLICIT,
    )
    with pytest.raises(ValidationError):
        EvidenceDossier(company_id=factories.COMPANY_ID, claims=[foreign], sources=[src])


def test_unknowns_and_conflicts_are_first_class() -> None:
    """Absence and disagreement have to survive into the artifact, not be dropped."""
    src, other = factories.source(), factories.source("https://news.example/write-up")
    bundle = EvidenceDossier(
        company_id=factories.COMPANY_ID,
        sources=[src, other],
        unknowns=[
            EvidenceUnknown(
                category=EvidenceCategory.TEAM,
                question="Who founded the company?",
                reason="No team page was published.",
            )
        ],
        conflicts=[
            EvidenceConflict(
                category=EvidenceCategory.TRACTION,
                summary="The two sources give different customer counts.",
                source_ids=[src.source_id, other.source_id],
            )
        ],
    )
    assert bundle.unknowns[0].category is EvidenceCategory.TEAM
    assert len(bundle.conflicts[0].source_ids) == 2


def test_a_conflict_must_cite_sources_the_dossier_carries() -> None:
    src, other = factories.source(), factories.source("https://news.example/write-up")
    with pytest.raises(ValidationError, match="conflict cites unknown source_ids"):
        EvidenceDossier(
            company_id=factories.COMPANY_ID,
            sources=[src],
            conflicts=[
                EvidenceConflict(
                    category=EvidenceCategory.MARKET,
                    summary="disagreement",
                    source_ids=[src.source_id, other.source_id],
                )
            ],
        )


# -- scoring -----------------------------------------------------------------


def _component(dimension: RubricDimension, **overrides: object) -> ScoreComponent:
    payload: dict[str, object] = {
        "component": dimension,
        "score": 1,
        "maximum": max_points_for(dimension),
        "assessment_status": AssessmentStatus.SUPPORTED,
        "rationale": "a rationale",
        "evidence_claim_ids": ["ev-000000000001"],
    }
    payload.update(overrides)
    return ScoreComponent(**payload)  # type: ignore[arg-type]


def test_a_component_cannot_exceed_its_configured_maximum() -> None:
    with pytest.raises(ValidationError, match="against a maximum"):
        _component(RubricDimension.TRACTION, score=11)


def test_a_component_cannot_declare_a_foreign_maximum() -> None:
    with pytest.raises(ValidationError, match="rubric configures"):
        _component(RubricDimension.TEAM, maximum=99)


@pytest.mark.parametrize(
    ("dimension", "status", "ceiling"),
    [
        (RubricDimension.PAIN_ROI, AssessmentStatus.SUPPORTED, 20),
        (RubricDimension.PAIN_ROI, AssessmentStatus.PARTIALLY_SUPPORTED, 14),
        (RubricDimension.PAIN_ROI, AssessmentStatus.NOT_ASSESSABLE, 10),
        (RubricDimension.TEAM, AssessmentStatus.PARTIALLY_SUPPORTED, 10),
        (RubricDimension.TEAM, AssessmentStatus.NOT_ASSESSABLE, 7),
        (RubricDimension.TRACTION, AssessmentStatus.PARTIALLY_SUPPORTED, 7),
        (RubricDimension.TRACTION, AssessmentStatus.NOT_ASSESSABLE, 5),
        (RubricDimension.MARKET_TIMING, AssessmentStatus.CONTRADICTED, 10),
    ],
)
def test_status_ceilings_are_floored_not_rounded_up(
    dimension: RubricDimension, status: AssessmentStatus, ceiling: int
) -> None:
    assert ceiling_for(dimension, status) == ceiling
    assert _component(dimension, score=ceiling, assessment_status=status).score == ceiling
    # When the ceiling equals the dimension's maximum the maximum check fires first, so
    # either rejection message is correct.
    with pytest.raises(ValidationError, match="may score at most|against a maximum"):
        _component(dimension, score=ceiling + 1, assessment_status=status)


def test_a_not_assessable_component_needs_no_evidence_and_is_not_forced_to_zero() -> None:
    """Absence of evidence caps the score; it does not zero it or condemn the company."""
    component = _component(
        RubricDimension.TEAM,
        score=4,
        assessment_status=AssessmentStatus.NOT_ASSESSABLE,
        evidence_claim_ids=[],
        unknown_references=["unk-000000000001"],
    )
    assert component.score == 4
    assert component.has_evidence is False


def test_a_supported_component_must_cite_evidence() -> None:
    with pytest.raises(ValidationError, match="must cite at least one evidence claim"):
        _component(RubricDimension.TEAM, evidence_claim_ids=[])


def test_a_contradicted_component_must_cite_the_contrary_evidence() -> None:
    with pytest.raises(ValidationError, match="must cite the contrary evidence"):
        _component(
            RubricDimension.TEAM,
            assessment_status=AssessmentStatus.CONTRADICTED,
            evidence_claim_ids=[],
        )


def test_total_score_must_equal_the_component_sum() -> None:
    subject = analysis_fixtures.analysis(analysis_fixtures.dossier(), total=40)
    tampered = subject.model_dump(mode="json") | {"total_score": 95}
    with pytest.raises(ValidationError, match="does not equal the component sum"):
        StartupAnalysis.model_validate(tampered)


def test_an_analysis_must_carry_all_seven_dimensions_exactly_once() -> None:
    subject = analysis_fixtures.analysis(analysis_fixtures.dossier(), total=40)
    payload = subject.model_dump(mode="json")
    payload["score_components"] = payload["score_components"][:5]
    payload["total_score"] = sum(c["score"] for c in payload["score_components"])
    with pytest.raises(ValidationError, match="missing rubric dimensions"):
        StartupAnalysis.model_validate(payload)


def test_scored_out_of_reports_what_was_actually_assessable() -> None:
    subject = analysis_fixtures.analysis(
        analysis_fixtures.dossier(), total=40, unassessable=(RubricDimension.TEAM,)
    )
    assert subject.scored_out_of == 100 - max_points_for(RubricDimension.TEAM)


def test_the_model_suggestion_is_stored_but_optional() -> None:
    bundle = analysis_fixtures.dossier()
    assert analysis_fixtures.analysis(bundle).model_suggested_recommendation is None
    suggested = analysis_fixtures.analysis(bundle, suggested=Recommendation.TAKE_A_MEETING)
    assert suggested.model_suggested_recommendation is Recommendation.TAKE_A_MEETING


def test_an_analysis_section_must_be_anchored_to_evidence_or_an_unknown() -> None:
    with pytest.raises(ValidationError, match="must cite evidence claim IDs"):
        AnalysisSection(text="An unsourced assertion.")
    assert AnalysisSection(text="Reasoning from a gap.", unknown_references=["unk-1"])


def test_a_risk_must_be_anchored_to_evidence_or_an_unknown() -> None:
    with pytest.raises(ValidationError, match="must cite evidence claim IDs"):
        RiskItem(text="An unsourced worry.")
    assert RiskItem(text="No pricing was published.", unknown_references=["unk-1"])


def test_a_competitive_observation_must_cite_evidence() -> None:
    with pytest.raises(ValidationError):
        CompetitiveObservation(text="They compete with everyone.", evidence_claim_ids=[])


def test_a_thesis_mismatch_must_cite_evidence_but_undetermined_need_not() -> None:
    with pytest.raises(ValidationError, match="must cite evidence"):
        ThesisAssessment(verdict=ThesisFit.MISMATCH, rationale="It is infrastructure.")
    assert ThesisAssessment(verdict=ThesisFit.UNDETERMINED, rationale="Nothing establishes fit.")


@pytest.mark.parametrize("count", [0, 1, 4])
def test_recommendation_changers_must_number_two_or_three(count: int) -> None:
    with pytest.raises(ValidationError, match="recommendation_changers must list"):
        analysis_fixtures.analysis(analysis_fixtures.dossier(), changers=count)


@pytest.mark.parametrize("count", [2, 3])
def test_two_or_three_recommendation_changers_are_accepted(count: int) -> None:
    assert analysis_fixtures.analysis(analysis_fixtures.dossier(), changers=count)


# -- extra keys --------------------------------------------------------------


def test_unexpected_keys_are_rejected_rather_than_dropped() -> None:
    payload = analysis_fixtures.analysis(analysis_fixtures.dossier()).model_dump(mode="json")
    payload["recommendation"] = "take_a_meeting"
    with pytest.raises(ValidationError):
        StartupAnalysis.model_validate(payload)
