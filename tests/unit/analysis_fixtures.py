"""Builders for the analysis stage.

Everything here is offline. The provider is always :class:`FakeProvider` or a scripted
payload, and no test in this suite can reach a network or read a credential.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from vc_scout.models.analysis import (
    AnalysisSection,
    ScoreComponent,
    StartupAnalysis,
    ThesisAssessment,
    ceiling_for,
)
from vc_scout.models.candidate import Candidate, CandidateSet
from vc_scout.models.enums import (
    AssessmentStatus,
    ConfidenceLevel,
    EvidenceCategory,
    InferenceStatus,
    Recommendation,
    RubricDimension,
    SourceKind,
    ThesisFit,
    VerificationStatus,
)
from vc_scout.models.evidence import (
    ConfidenceInputs,
    EvidenceClaim,
    EvidenceConflict,
    EvidenceDossier,
    EvidenceUnknown,
    SourceCoverage,
    SupportingExcerpt,
)
from vc_scout.models.recommendation import ResearchConfidence
from vc_scout.models.source import SourceReference
from vc_scout.prompts import prompt_sha256
from vc_scout.rubric import RUBRIC, max_points_for
from vc_scout.store import RunStore
from vc_scout.thesis import THESIS_VERSION
from vc_scout.util.ids import unknown_id_for

__all__ = [
    "NOW",
    "analysis",
    "analysis_payload",
    "component_payload",
    "section_payload",
    "dossier",
    "seed_run",
    "unknown_ref",
]

NOW = datetime(2026, 8, 26, 9, 0, tzinfo=UTC)
COMPANY = "acme-ops"


def _source(url: str, kind: SourceKind = SourceKind.COMPANY_PAGE) -> SourceReference:
    return SourceReference.create(url, kind=kind)


def dossier(
    *,
    company_id: str = COMPANY,
    claims: int = 6,
    categories: tuple[EvidenceCategory, ...] | None = None,
    verification: VerificationStatus = VerificationStatus.COMPANY_CLAIM,
    unknowns: int = 2,
    conflicts: int = 0,
    website_available: bool = True,
    sources_supplied: int | None = None,
    sources_cited: int | None = None,
    extra_claims: list[EvidenceClaim] | None = None,
) -> EvidenceDossier:
    """An evidence dossier with controllable coverage, for confidence and policy tests."""
    spread = categories or (
        EvidenceCategory.PRODUCT,
        EvidenceCategory.MARKET,
        EvidenceCategory.TRACTION,
    )
    home = _source(f"https://{company_id}.example/")
    other = _source(f"https://{company_id}.example/pricing")
    hn = _source("https://news.ycombinator.com/item?id=1", kind=SourceKind.HN_STORY)

    built: list[EvidenceClaim] = []
    for index in range(claims):
        built.append(
            EvidenceClaim.create(
                company_id=company_id,
                category=spread[index % len(spread)],
                claim=f"Recorded finding number {index} about the company.",
                excerpts=[
                    SupportingExcerpt(
                        source_id=home.source_id, excerpt=f"finding number {index} text"
                    )
                ],
                verification_status=verification,
                inference_status=InferenceStatus.EXPLICIT,
            )
        )
    built.extend(extra_claims or [])

    return EvidenceDossier(
        company_id=company_id,
        claims=built,
        unknowns=[
            EvidenceUnknown(
                category=EvidenceCategory.TEAM, question=f"Open question number {index}?"
            )
            for index in range(unknowns)
        ],
        conflicts=[
            EvidenceConflict(
                category=EvidenceCategory.TRACTION,
                summary=f"Sources disagree about point {index}.",
                source_ids=[home.source_id, other.source_id],
            )
            for index in range(conflicts)
        ],
        sources=[home, other, hn],
        source_coverage=SourceCoverage(
            sources_supplied=sources_supplied if sources_supplied is not None else 3,
            sources_cited=sources_cited if sources_cited is not None else (1 if built else 0),
            pages_supplied=2,
            website_available=website_available,
            hn_sources=1,
        ),
        confidence_inputs=ConfidenceInputs(claims_total=len(built)),
        prompt_version="evidence_v1",
        provider="fake",
        model="fake-model-1",
        generated_at=NOW,
    )


def unknown_ref(bundle: EvidenceDossier, index: int = 0) -> str:
    return unknown_id_for(bundle.company_id, bundle.unknowns[index].question)


def _components(
    bundle: EvidenceDossier,
    *,
    total: int,
    status: AssessmentStatus,
    unassessable: tuple[RubricDimension, ...] = (),
) -> list[ScoreComponent]:
    """Seven components summing to ``total``, distributed in rubric order."""
    claim_id = bundle.claims[0].claim_id if bundle.claims else None
    remaining = total
    built: list[ScoreComponent] = []
    for spec in RUBRIC:
        this_status = (
            AssessmentStatus.NOT_ASSESSABLE
            if spec.key in unassessable or claim_id is None
            else status
        )
        ceiling = ceiling_for(spec.key, this_status)
        award = min(ceiling, remaining)
        remaining -= award
        needs_evidence = this_status in (
            AssessmentStatus.SUPPORTED,
            AssessmentStatus.PARTIALLY_SUPPORTED,
            AssessmentStatus.CONTRADICTED,
        )
        built.append(
            ScoreComponent(
                component=spec.key,
                score=award,
                maximum=max_points_for(spec.key),
                assessment_status=this_status,
                rationale="fixture rationale explaining the score and the uncertainty",
                evidence_claim_ids=[claim_id] if needs_evidence and claim_id else [],
                unknown_references=(
                    [unknown_ref(bundle)]
                    if this_status is AssessmentStatus.NOT_ASSESSABLE and bundle.unknowns
                    else []
                ),
            )
        )
    if remaining:
        raise ValueError(f"cannot distribute {total} points under {status.value}")
    return built


def analysis(
    bundle: EvidenceDossier,
    *,
    total: int = 50,
    status: AssessmentStatus = AssessmentStatus.SUPPORTED,
    unassessable: tuple[RubricDimension, ...] = (),
    confidence: ResearchConfidence | None = None,
    buyer: str | None = "Small plumbing contractors",
    workflow: str | None = "Invoice reconciliation",
    product: str = "An AI agent that reconciles invoices.",
    thesis_verdict: ThesisFit = ThesisFit.ALIGNED,
    thesis_evidence: bool = True,
    identity_warnings: tuple[str, ...] = (),
    suggested: Recommendation | None = None,
    corroborated: int = 0,
    changers: int = 2,
) -> StartupAnalysis:
    """A valid analysis over ``bundle``, with the knobs the policy tests need."""
    claim_id = bundle.claims[0].claim_id if bundle.claims else None
    anchored = (
        {"evidence_claim_ids": [claim_id]}
        if claim_id
        else {"unknown_references": [unknown_ref(bundle)]}
    )
    section = AnalysisSection(text="Fixture assessment text.", **anchored)  # type: ignore[arg-type]
    return StartupAnalysis(
        company_id=bundle.company_id,
        thesis_version=THESIS_VERSION,
        prompt_version="analysis_v1",
        provider="fake",
        model="fake-model-1",
        plain_language_product=product,
        buyer=buyer,
        workflow=workflow,
        team_assessment=section,
        product_assessment=section,
        market_assessment=section,
        thesis_assessment=ThesisAssessment(
            verdict=thesis_verdict,
            rationale="Fixture thesis rationale.",
            evidence_claim_ids=[claim_id] if thesis_evidence and claim_id else [],
            unknown_references=[] if thesis_evidence and claim_id else [unknown_ref(bundle)],
        ),
        corroborated_findings=[
            {"fact": f"Corroborated fact {i}", "evidence_claim_ids": [claim_id]}  # type: ignore[list-item]
            for i in range(corroborated)
            if claim_id
        ],
        score_components=_components(bundle, total=total, status=status, unassessable=unassessable),
        total_score=total,
        research_confidence=confidence or ResearchConfidence(level=ConfidenceLevel.HIGH, score=0.9),
        model_suggested_recommendation=suggested,
        recommendation_changers=[f"Diligence item {i}" for i in range(changers)],
        identity_warnings=list(identity_warnings),
        generated_at=NOW,
    )


# -- model-shaped payloads (for validation tests) ----------------------------


def component_payload(
    dimension: RubricDimension,
    *,
    score: int,
    status: str = "supported",
    claim_id: str | None = None,
    unknown: str | None = None,
) -> dict[str, Any]:
    """One score component in the compact provider shape - no `maximum`."""
    return {
        "component": dimension.value,
        "score": score,
        "assessment_status": status,
        "rationale": "How the score reflects the evidence and the remaining uncertainty.",
        "evidence_claim_ids": [claim_id] if claim_id else [],
        "unknown_references": [unknown] if unknown else [],
        "caveats": [],
    }


def section_payload(
    kind: str, *, claim_id: str | None = None, unknown: str | None = None, text: str | None = None
) -> dict[str, Any]:
    return {
        "kind": kind,
        "text": text or f"Fixture {kind} text.",
        "evidence_claim_ids": [claim_id] if claim_id else [],
        "unknown_references": [unknown] if unknown else [],
    }


def analysis_payload(
    bundle: EvidenceDossier,
    *,
    scores: dict[RubricDimension, int] | None = None,
    status: str = "supported",
    changers: int = 2,
    extra_sections: list[dict[str, Any]] | None = None,
    **overrides: Any,
) -> dict[str, Any]:
    """A model-shaped analysis payload over ``bundle``, valid unless overridden."""
    claim_id = bundle.claims[0].claim_id if bundle.claims else None
    unknown = unknown_ref(bundle) if bundle.unknowns else None
    effective_status = status if claim_id else "not_assessable"
    anchor = {"claim_id": claim_id} if claim_id else {"unknown": unknown}

    payload: dict[str, Any] = {
        "plain_language_product": "An AI agent that reconciles invoices for contractors.",
        "buyer": "Small plumbing contractors",
        "workflow": "Invoice reconciliation",
        "thesis_fit": "aligned" if claim_id else "undetermined",
        "sections": [
            section_payload(kind, **anchor)  # type: ignore[arg-type]
            for kind in ("team", "product", "market", "thesis")
        ]
        + (extra_sections or []),
        "score_components": [
            component_payload(
                spec.key,
                score=(scores or {}).get(spec.key, 5 if claim_id else 0),
                status=effective_status,
                claim_id=claim_id if effective_status != "not_assessable" else None,
                unknown=unknown if effective_status == "not_assessable" else None,
            )
            for spec in RUBRIC
        ],
        "open_questions": ["What is the retention rate?"],
        "model_suggested_recommendation": "watch",
        "recommendation_changers": [f"Diligence item {i}" for i in range(changers)],
        "identity_warnings": [],
        "analysis_warnings": [],
    }
    payload.update(overrides)
    return payload


def seed_run(store: RunStore, bundles: list[EvidenceDossier]) -> CandidateSet:
    """Write candidates and evidence dossiers for the analysis stage to read."""
    store.ensure_root()
    candidates, sources = [], []
    for index, bundle in enumerate(bundles):
        hn = SourceReference.create(
            f"https://news.ycombinator.com/item?id=70{index}", kind=SourceKind.HN_STORY
        )
        sources.append(hn)
        candidates.append(
            Candidate(
                company_id=bundle.company_id,
                name=bundle.company_id.replace("-", " ").title(),
                source_ids=[hn.source_id],
                one_liner="AI agent for invoicing",
                website=f"https://{bundle.company_id}.example/",
            )
        )
        store.write_evidence(bundle)
    bundle_set = CandidateSet(
        run_id=store.run_id,
        query="AI agents for SMB operations",
        candidates=candidates,
        sources=sources,
        generated_at=NOW,
    )
    store.write_candidates(bundle_set)
    return bundle_set


assert prompt_sha256("analysis_v1")  # the prompt file must exist for these fixtures
