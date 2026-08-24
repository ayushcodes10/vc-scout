"""The deterministic provider's derived responses.

Regression cover for the first offline acceptance run of the analysis stage, which failed
for all fifteen candidates: the provider returned one hard-coded evidence-shaped payload
regardless of which tool the request asked for, and the analysis validator - correctly -
rejected it.

Everything a derived response cites must have been offered by the request. These tests run
that output through the **production** validators, not a relaxed copy.
"""

from __future__ import annotations

import pytest

from tests.unit.analysis_fixtures import dossier, unknown_ref
from vc_scout.llm.analysis_schema import ANALYSIS_SCHEMA, ANALYSIS_TOOL_NAME
from vc_scout.llm.analysis_validation import (
    AnalysisValidationError,
    find_unsupported_market_numbers,
    index_dossier,
    validate_analysis,
)
from vc_scout.llm.fake import FakeProvider, derive_response
from vc_scout.llm.provider import LlmRequest, ModelConfig
from vc_scout.llm.schema import EVIDENCE_SCHEMA, EVIDENCE_TOOL_NAME
from vc_scout.llm.validation import SuppliedSource, validate_evidence
from vc_scout.models.candidate import Candidate
from vc_scout.models.enums import AssessmentStatus, Recommendation, SourceKind
from vc_scout.models.evidence import EvidenceDossier
from vc_scout.models.source import SourceReference
from vc_scout.rubric import RUBRIC
from vc_scout.stages.analysis import render_dossier_payload

CONFIG = ModelConfig(model="fake-model-1", max_tokens=4096)


def candidate(company_id: str) -> Candidate:
    hn = SourceReference.create("https://news.ycombinator.com/item?id=1", kind=SourceKind.HN_STORY)
    return Candidate(
        company_id=company_id,
        name=company_id.replace("-", " ").title(),
        source_ids=[hn.source_id],
        website=f"https://{company_id}.example/",
    )


def analysis_request(bundle: EvidenceDossier, attempt: int = 1) -> LlmRequest:
    """A request built exactly as the production stage builds it."""
    return LlmRequest(
        system="System instructions.",
        user_payload=render_dossier_payload(candidate(bundle.company_id), bundle),
        schema=ANALYSIS_SCHEMA,
        schema_name=ANALYSIS_TOOL_NAME,
        schema_description="Record your assessment.",
        config=CONFIG,
        attempt=attempt,
    )


def evidence_request() -> LlmRequest:
    return LlmRequest(
        system="System instructions.",
        user_payload="## Sources\n(none)",
        schema=EVIDENCE_SCHEMA,
        schema_name=EVIDENCE_TOOL_NAME,
        schema_description="Record the evidence.",
        config=CONFIG,
    )


# -- the defect --------------------------------------------------------------


def test_the_derived_response_matches_the_tool_that_was_requested() -> None:
    """The regression: one hard-coded shape answered both tools."""
    bundle = dossier(claims=4)
    analysis = derive_response(analysis_request(bundle))
    evidence = derive_response(evidence_request())

    assert "score_components" in analysis
    assert "claims" not in analysis
    assert "claims" in evidence
    assert "score_components" not in evidence


def test_the_evidence_response_still_validates_against_its_own_validator() -> None:
    source = SourceReference.create("https://acme.example/", kind=SourceKind.COMPANY_PAGE)
    outcome = validate_evidence(
        derive_response(evidence_request()),
        company_id="acme-ops",
        sources=[SuppliedSource(reference=source, text="page text", role="homepage")],
        prompt_version="evidence_v1",
        provider="fake",
        model="fake-model-1",
        generated_at=dossier().generated_at,
        website_available=True,
    )
    assert outcome.dossier.claims == []


# -- derived analyses pass the production validator --------------------------


@pytest.mark.parametrize("claims", [1, 2, 4, 6, 8, 11])
def test_a_derived_analysis_validates_for_any_evidence_volume(claims: int) -> None:
    bundle = dossier(claims=claims, unknowns=2)
    result = validate_analysis(derive_response(analysis_request(bundle)), dossier=bundle)

    assert len(result.score_components) == len(RUBRIC) == 7
    assert result.total_score == sum(c.score for c in result.score_components)
    assert {c.component for c in result.score_components} == {spec.key for spec in RUBRIC}
    assert {c.maximum for c in result.score_components} == {spec.max_points for spec in RUBRIC}
    assert 2 <= len(result.recommendation_changers) <= 3
    assert result.model_suggested_recommendation in set(Recommendation)


def test_every_component_respects_its_status_ceiling() -> None:
    bundle = dossier(claims=11)
    result = validate_analysis(derive_response(analysis_request(bundle)), dossier=bundle)
    for component in result.score_components:
        assert component.score <= component.ceiling
        assert component.score <= component.maximum


def test_only_supplied_identifiers_are_ever_cited() -> None:
    bundle = dossier(claims=4, unknowns=3)
    index = index_dossier(bundle)
    result = validate_analysis(derive_response(analysis_request(bundle)), dossier=bundle)

    cited = {
        cid for component in result.score_components for cid in component.evidence_claim_ids
    } | set(result.team_assessment.evidence_claim_ids)
    referenced = {
        ref for component in result.score_components for ref in component.unknown_references
    } | set(result.team_assessment.unknown_references)

    assert cited <= index.claim_ids
    assert referenced <= index.unknown_ids


def test_supported_components_cite_distinct_claims() -> None:
    """A dimension is only supported while there is a distinct claim to anchor it."""
    bundle = dossier(claims=3)
    result = validate_analysis(derive_response(analysis_request(bundle)), dossier=bundle)
    supported = [
        c for c in result.score_components if c.assessment_status is AssessmentStatus.SUPPORTED
    ]
    assert len(supported) == 3
    assert len({c.evidence_claim_ids[0] for c in supported}) == 3


def test_no_market_size_figures_or_competitors_are_invented() -> None:
    bundle = dossier(claims=6)
    result = validate_analysis(derive_response(analysis_request(bundle)), dossier=bundle)
    narrative = " ".join(
        [
            result.plain_language_product,
            result.team_assessment.text,
            result.thesis_assessment.rationale,
            *(c.rationale for c in result.score_components),
            *(r.text for r in result.risks),
            *result.recommendation_changers,
        ]
    )
    assert find_unsupported_market_numbers(narrative, corpus="") == []
    assert result.competitive_observations == []
    assert result.corroborated_findings == []


def test_the_derived_response_is_deterministic() -> None:
    bundle = dossier(claims=5)
    assert derive_response(analysis_request(bundle)) == derive_response(analysis_request(bundle))


def test_richer_dossiers_produce_higher_derived_totals() -> None:
    thin, rich = dossier(claims=2), dossier(claims=8)
    thin_total = validate_analysis(
        derive_response(analysis_request(thin)), dossier=thin
    ).total_score
    rich_total = validate_analysis(
        derive_response(analysis_request(rich)), dossier=rich
    ).total_score
    assert thin_total < rich_total


# -- zero-claim dossiers -----------------------------------------------------


def test_a_zero_claim_dossier_yields_an_insufficient_evidence_analysis() -> None:
    bundle = dossier(claims=0, unknowns=4)
    result = validate_analysis(derive_response(analysis_request(bundle)), dossier=bundle)

    assert result.total_score == 0
    assert all(
        c.assessment_status is AssessmentStatus.NOT_ASSESSABLE for c in result.score_components
    )
    # Nothing is fabricated: no claim is cited, because none was offered.
    assert all(c.evidence_claim_ids == [] for c in result.score_components)
    assert all(c.unknown_references == [unknown_ref(bundle)] for c in result.score_components)
    assert result.thesis_assessment.verdict.value == "undetermined"
    assert any("not about the company" in w for w in result.analysis_warnings)


def test_a_zero_claim_dossier_with_no_unknowns_still_validates() -> None:
    bundle = dossier(claims=0, unknowns=0)
    payload = derive_response(analysis_request(bundle))
    # An unanchored section would be rejected; there is nothing to anchor to, so the
    # production validator is the right place for this to surface.
    with pytest.raises(AnalysisValidationError, match="must cite evidence claim IDs"):
        validate_analysis(payload, dossier=bundle)


# -- through the provider ----------------------------------------------------


def test_the_provider_returns_the_derived_response_when_unscripted() -> None:
    bundle = dossier(claims=4)
    provider = FakeProvider()
    result = provider.complete_json(analysis_request(bundle))

    assert provider.call_count == 1
    assert "score_components" in result.content
    assert result.stop_reason == "tool_use"
    validate_analysis(result.content, dossier=bundle)


def test_a_scripted_response_still_takes_precedence() -> None:
    bundle = dossier(claims=4)
    provider = FakeProvider([{"score_components": "scripted"}])
    assert provider.complete_json(analysis_request(bundle)).content == {
        "score_components": "scripted"
    }
