"""Research confidence and the binding recommendation.

Nothing here calls a language model, touches the network or reads an ambient clock. Given
the same analysis and the same dossier it always returns the same recommendation - that is
the point. A model may *suggest* a call; this is the module that makes one.

Two computations live here, and they answer different questions:

* **Research confidence** - how much did we actually find out? A statement about the
  research, never about the company.
* **The recommendation** - what should a partner do, given the score band and a set of
  guardrails that stop a confident-sounding narrative on thin evidence from producing a
  meeting, and stop an absence of evidence from producing a pass.
"""

from __future__ import annotations

from datetime import datetime

from vc_scout.models.analysis import StartupAnalysis
from vc_scout.models.enums import (
    AssessmentStatus,
    ConfidenceLevel,
    EvidenceCategory,
    Recommendation,
)
from vc_scout.models.evidence import EvidenceDossier
from vc_scout.models.recommendation import RecommendationResult, ResearchConfidence

__all__ = [
    "CONFIDENCE_HIGH_AT",
    "CONFIDENCE_MEDIUM_AT",
    "POLICY_VERSION",
    "TAKE_A_MEETING_AT",
    "WATCH_AT",
    "Guardrail",
    "band_for",
    "compute_confidence",
    "decide",
]

POLICY_VERSION = "2.0.0"

#: Score bands, inclusive lower bounds. 80-100 take a meeting, 65-79 watch, 0-64 pass.
TAKE_A_MEETING_AT = 80
WATCH_AT = 65

#: Confidence bands, inclusive lower bounds.
CONFIDENCE_HIGH_AT = 0.65
CONFIDENCE_MEDIUM_AT = 0.40

# --------------------------------------------------------------------------
# Research confidence
# --------------------------------------------------------------------------
#
# Six positive components, weighted to sum to 1.0, then bounded penalties. Every input is
# a countable property of the dossier and the analysis - none is supplied by the model.

_W_SOURCE_COVERAGE = 0.15
_W_CLAIM_VOLUME = 0.20
_W_CATEGORY_SPAN = 0.20
_W_CORROBORATION = 0.15
_W_WEBSITE = 0.15
_W_INDEPENDENCE = 0.15
assert (
    abs(
        _W_SOURCE_COVERAGE
        + _W_CLAIM_VOLUME
        + _W_CATEGORY_SPAN
        + _W_CORROBORATION
        + _W_WEBSITE
        + _W_INDEPENDENCE
        - 1.0
    )
    < 1e-9
)

#: Claims beyond this stop adding confidence; a long dossier is not a better-researched one.
_CLAIM_SATURATION = 8
#: Corroborated findings beyond this stop adding confidence.
_CORROBORATION_SATURATION = 3

#: Bounded penalties. Each is capped so no single factor can dominate the score.
_PENALTY_PER_IDENTITY_WARNING = 0.25
_PENALTY_IDENTITY_CAP = 0.25
_PENALTY_PER_CONFLICT = 0.05
_PENALTY_CONFLICT_CAP = 0.10
_PENALTY_PER_UNKNOWN = 0.01
_PENALTY_UNKNOWN_CAP = 0.10


class Guardrail:
    """Stable identifiers for the policy rules that can move a recommendation."""

    ZERO_CLAIM_DOSSIER = "zero_claim_dossier"
    INSUFFICIENT_EVIDENCE = "insufficient_evidence_watch"
    IDENTITY_MISMATCH_CAP = "identity_mismatch_cap"
    MEETING_NEEDS_CONFIDENCE = "meeting_requires_medium_confidence"
    MEETING_NEEDS_PRODUCT = "meeting_requires_identifiable_product"
    MEETING_NEEDS_BUYER = "meeting_requires_identifiable_buyer"
    MEETING_NEEDS_BREADTH = "meeting_requires_four_evidenced_dimensions"


def band_for(total_score: int) -> Recommendation:
    """Map a total score to its band. The only place thresholds are applied."""
    if total_score >= TAKE_A_MEETING_AT:
        return Recommendation.TAKE_A_MEETING
    if total_score >= WATCH_AT:
        return Recommendation.WATCH
    return Recommendation.PASS


def _level(score: float) -> ConfidenceLevel:
    if score >= CONFIDENCE_HIGH_AT:
        return ConfidenceLevel.HIGH
    if score >= CONFIDENCE_MEDIUM_AT:
        return ConfidenceLevel.MEDIUM
    return ConfidenceLevel.LOW


def compute_confidence(
    analysis: StartupAnalysis | None,
    dossier: EvidenceDossier,
    *,
    identity_warnings: int = 0,
) -> ResearchConfidence:
    """Score how well-researched a company is, from countable coverage facts alone.

    The formula, in full::

        source_coverage = cited sources / supplied sources
        claim_volume    = min(claims / 8, 1)
        category_span   = distinct evidence categories / 5
        corroboration   = min(corroborated findings / 3, 1)
        website         = 1 if a website page was read, else 0
        independence    = share of claims that are not company_claim

        score = 0.15*source_coverage + 0.20*claim_volume + 0.20*category_span
              + 0.15*corroboration  + 0.15*website      + 0.15*independence

        penalties = min(0.25 * identity_warnings, 0.25)
                  + min(0.05 * conflicts,         0.10)
                  + min(0.01 * unknowns,          0.10)

        confidence = clamp(score - penalties, 0.0, 1.0)

    A dossier with no claims scores 0.0 outright: nothing was established, and no
    combination of coverage facts should be able to dress that up.

    ``corroboration`` counts only the findings the analysis explicitly named as
    corroborated. The ``independently_supported`` label on a claim earns nothing on its
    own - the first live run produced one that was two sources supporting different halves
    of a compound statement.
    """
    coverage = dossier.source_coverage
    supplied = coverage.sources_supplied if coverage else len(dossier.sources)
    cited = coverage.sources_cited if coverage else 0
    website_available = coverage.website_available if coverage else False

    claims = len(dossier.claims)
    reasons: list[str] = []

    if claims == 0:
        reasons.append(
            "The dossier contains no evidence claims, so nothing about this company was "
            "established. This is a statement about the research, not about the company."
        )
        if identity_warnings:
            reasons.append(f"{identity_warnings} identity warning(s) were recorded.")
        return ResearchConfidence(
            level=ConfidenceLevel.LOW,
            score=0.0,
            components={
                "source_coverage": 0.0,
                "claim_volume": 0.0,
                "category_span": 0.0,
                "corroboration": 0.0,
                "website": 1.0 if website_available else 0.0,
                "independence": 0.0,
            },
            reasons=reasons,
            missing=_missing(dossier, website_available),
        )

    source_coverage = cited / supplied if supplied else 0.0
    claim_volume = min(claims / _CLAIM_SATURATION, 1.0)
    categories = {claim.category for claim in dossier.claims}
    category_span = len(categories) / len(EvidenceCategory)
    corroborated = len(analysis.corroborated_findings) if analysis else 0
    corroboration = min(corroborated / _CORROBORATION_SATURATION, 1.0)
    website = 1.0 if website_available else 0.0
    company_claims = sum(
        1 for claim in dossier.claims if claim.verification_status.value == "company_claim"
    )
    independence = 1.0 - (company_claims / claims)

    raw = (
        _W_SOURCE_COVERAGE * source_coverage
        + _W_CLAIM_VOLUME * claim_volume
        + _W_CATEGORY_SPAN * category_span
        + _W_CORROBORATION * corroboration
        + _W_WEBSITE * website
        + _W_INDEPENDENCE * independence
    )
    penalties = (
        min(_PENALTY_PER_IDENTITY_WARNING * identity_warnings, _PENALTY_IDENTITY_CAP)
        + min(_PENALTY_PER_CONFLICT * len(dossier.conflicts), _PENALTY_CONFLICT_CAP)
        + min(_PENALTY_PER_UNKNOWN * len(dossier.unknowns), _PENALTY_UNKNOWN_CAP)
    )
    score = round(min(max(raw - penalties, 0.0), 1.0), 4)

    reasons = [
        f"{cited} of {supplied} supplied source(s) were cited by at least one claim.",
        f"{claims} evidence claim(s) across {len(categories)} of "
        f"{len(EvidenceCategory)} categories.",
        (
            "A page from the company's own website was read."
            if website_available
            else "No page from the company's own website could be read."
        ),
        f"{company_claims} of {claims} claim(s) are company-authored; "
        f"{claims - company_claims} come from other voices.",
        (
            f"{corroborated} finding(s) were identified as genuinely corroborated."
            if corroborated
            else "No finding was identified as corroborated by separate sources."
        ),
    ]
    if dossier.conflicts:
        reasons.append(
            f"{len(dossier.conflicts)} unresolved conflict(s) between sources were retained."
        )
    if dossier.unknowns:
        reasons.append(f"{len(dossier.unknowns)} question(s) were left unanswered.")
    if identity_warnings:
        reasons.append(
            f"{identity_warnings} identity warning(s): the sources may not describe this company."
        )

    return ResearchConfidence(
        level=_level(score),
        score=score,
        components={
            "source_coverage": round(source_coverage, 4),
            "claim_volume": round(claim_volume, 4),
            "category_span": round(category_span, 4),
            "corroboration": round(corroboration, 4),
            "website": website,
            "independence": round(independence, 4),
        },
        reasons=reasons,
        missing=_missing(dossier, website_available),
    )


def _missing(dossier: EvidenceDossier, website_available: bool) -> list[str]:
    missing = sorted({unknown.category.value for unknown in dossier.unknowns})
    if not website_available:
        missing.append("company_website")
    if not dossier.claims:
        missing.append("all_evidence")
    return missing


# --------------------------------------------------------------------------
# The recommendation
# --------------------------------------------------------------------------

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

#: Above this many unassessable dimensions, a low score is more likely to be an evidence
#: problem than a company problem.
_NOT_ASSESSABLE_FOR_INSUFFICIENT = 3
#: A meeting needs the evidence to have reached this many dimensions.
_MEETING_MIN_DIMENSIONS = 4


def decide(
    analysis: StartupAnalysis,
    dossier: EvidenceDossier,
    confidence: ResearchConfidence,
    *,
    decided_at: datetime | None = None,
) -> RecommendationResult:
    """Apply the recommendation policy. This is the binding call.

    ``analysis.model_suggested_recommendation`` is copied into the result for auditing and
    is never read as an input to the decision.
    """
    band = band_for(analysis.total_score)
    decision = band
    guardrails: list[str] = []
    rationale = [
        f"Scored {analysis.total_score}/100 against the rubric, which falls in the "
        f"{_BAND_LABELS[band]} band ({band.value}).",
        f"Research confidence is {confidence.level.value} ({confidence.score:.2f}).",
    ]

    unassessable = analysis.components_with_status(AssessmentStatus.NOT_ASSESSABLE)
    if unassessable:
        named = ", ".join(c.component.value for c in unassessable)
        if analysis.scored_out_of == 0:
            # Every dimension was unassessable, so there is no denominator to quote. Saying
            # "N of 0 points were assessable" alongside a positive total reads as nonsense,
            # and this sentence is what a memo renders.
            rationale.append(
                f"No dimension could be assessed from the available evidence: {named}. "
                "The points shown reflect residual uncertainty, not established merit."
            )
        else:
            rationale.append(
                f"{len(unassessable)} dimension(s) could not be assessed from the available "
                f"evidence: {named}. Only {analysis.scored_out_of} of 100 points were "
                "assessable."
            )

    # 1. Nothing was established at all. No score narrative is meaningful here.
    if not dossier.claims:
        decision = Recommendation.WATCH
        guardrails.append(Guardrail.ZERO_CLAIM_DOSSIER)
        rationale.append(
            "No evidence claims could be extracted for this company, so there is no basis "
            "for either a positive or a negative call. Recommending watch on insufficient "
            "evidence rather than passing on an absence of information."
        )

    # 2. A low score driven by missing evidence is not a reason to pass - unless the
    #    evidence positively shows the company is outside the thesis.
    elif (
        band is Recommendation.PASS
        and len(unassessable) > _NOT_ASSESSABLE_FOR_INSUFFICIENT
        and confidence.level is ConfidenceLevel.LOW
        and not analysis.thesis_assessment.is_supported_mismatch
    ):
        decision = Recommendation.WATCH
        guardrails.append(Guardrail.INSUFFICIENT_EVIDENCE)
        rationale.append(
            f"The score is low, but {len(unassessable)} dimensions were unassessable and "
            "research confidence is low, with no evidence that the company sits outside "
            "the thesis. That is an evidence shortfall, not a judgment; recommending watch."
        )

    # 3. A meeting is a claim on a partner's time and has to clear more than a number.
    if decision is Recommendation.TAKE_A_MEETING:
        failures: list[tuple[str, str]] = []
        if confidence.level is ConfidenceLevel.LOW:
            failures.append((Guardrail.MEETING_NEEDS_CONFIDENCE, "research confidence is low"))
        if not analysis.plain_language_product.strip():
            failures.append((Guardrail.MEETING_NEEDS_PRODUCT, "no product could be identified"))
        if not (analysis.buyer or "").strip():
            failures.append((Guardrail.MEETING_NEEDS_BUYER, "no buyer could be identified"))
        evidenced = analysis.dimensions_with_evidence()
        if len(evidenced) < _MEETING_MIN_DIMENSIONS:
            failures.append(
                (
                    Guardrail.MEETING_NEEDS_BREADTH,
                    f"evidence reached only {len(evidenced)} of the "
                    f"{_MEETING_MIN_DIMENSIONS} dimensions a meeting requires",
                )
            )
        if failures:
            decision = Recommendation.WATCH
            guardrails.extend(name for name, _ in failures)
            rationale.append(
                "The score reaches the meeting band, but "
                + "; ".join(reason for _, reason in failures)
                + ". Holding at watch until that is resolved."
            )

    # 4. If the sources may not even describe this company, no meeting can be justified.
    #    Evaluated against the *band* rather than the running decision, so the constraint is
    #    recorded even when another guardrail has already moved the call - an identity
    #    mismatch is something a reader must see, not something to lose behind a tie.
    if analysis.identity_warnings and _RANK[band] > _RANK[Recommendation.WATCH]:
        decision = min(decision, Recommendation.WATCH, key=lambda value: _RANK[value])
        guardrails.append(Guardrail.IDENTITY_MISMATCH_CAP)
        rationale.append(
            "An unresolved identity mismatch was recorded - the supplied sources may "
            "describe a different company - which caps the recommendation at watch until "
            "the candidate's identity is confirmed."
        )

    capped = _RANK[decision] < _RANK[band]
    suggested = analysis.model_suggested_recommendation
    return RecommendationResult(
        company_id=analysis.company_id,
        decision=decision,
        total_score=analysis.total_score,
        confidence=confidence,
        policy_version=POLICY_VERSION,
        band=band,
        band_label=_BAND_LABELS[band],
        capped=capped,
        cap_reason="; ".join(guardrails) if capped else None,
        rationale=rationale,
        guardrails_applied=guardrails,
        model_suggested=suggested,
        model_disagreed=None if suggested is None else suggested is not decision,
        decided_at=decided_at,
    )
