"""The deterministic one-sentence call, and how a memo explains it.

The recommendation is already made - :mod:`vc_scout.policy` made it, from the score, the
confidence and its guardrails. What is left is saying it in one sentence a partner can
read in three seconds, and that sentence is assembled here from the recorded facts. No
language model writes it, and none is asked to: a memo whose headline sentence came from a
model would be a fourth opinion sitting on top of three deterministic ones.

The distinction the wording carries is the one this whole pipeline exists to protect. A
pass because the evidence shows the company is outside the thesis, and a watch because the
research established too little to judge, are opposite findings that a naive renderer would
flatten into "low score". They are labelled separately here, and never phrased so that
missing evidence reads as a weakness.
"""

from __future__ import annotations

from enum import StrEnum

from vc_scout.models.analysis import StartupAnalysis
from vc_scout.models.enums import AssessmentStatus, Recommendation
from vc_scout.models.evidence import EvidenceDossier
from vc_scout.models.recommendation import RecommendationResult
from vc_scout.policy import TAKE_A_MEETING_AT, WATCH_AT, Guardrail
from vc_scout.rubric import RUBRIC

__all__ = [
    "CallKind",
    "GUARDRAIL_CHIPS",
    "SHORT_RATIONALE",
    "call_label",
    "DECISION_LABELS",
    "GUARDRAIL_LABELS",
    "call_kind",
    "call_sentence",
    "reading",
]

DECISION_LABELS: dict[Recommendation, str] = {
    Recommendation.TAKE_A_MEETING: "Take a meeting",
    Recommendation.WATCH: "Watch",
    Recommendation.PASS: "Pass",
}

#: Guardrail identifiers in the language of a memo, not of the policy module.
GUARDRAIL_LABELS: dict[str, str] = {
    Guardrail.ZERO_CLAIM_DOSSIER: (
        "No evidence claim could be extracted at all, so there is no basis for a positive "
        "or a negative call. The policy holds at watch rather than passing on an absence."
    ),
    Guardrail.INSUFFICIENT_EVIDENCE: (
        "The low score is driven by dimensions the evidence could not reach, so the policy "
        "holds at watch rather than reading a research shortfall as a judgement."
    ),
    Guardrail.IDENTITY_MISMATCH_CAP: (
        "The sources may describe a different company, which caps the call at watch until "
        "the candidate's identity is confirmed."
    ),
    Guardrail.MEETING_NEEDS_CONFIDENCE: (
        "A meeting requires at least medium research confidence; this run did not reach it."
    ),
    Guardrail.MEETING_NEEDS_PRODUCT: (
        "A meeting requires an identifiable product, which the evidence did not establish."
    ),
    Guardrail.MEETING_NEEDS_BUYER: (
        "A meeting requires an identifiable buyer, which the evidence did not establish."
    ),
    Guardrail.MEETING_NEEDS_BREADTH: (
        "A meeting requires evidence across at least four rubric dimensions."
    ),
}


class CallKind(StrEnum):
    """Why the call is what it is. Drives the wording, never the decision."""

    EVIDENCE_BACKED_MEETING = "evidence_backed_meeting"
    #: The evidence positively shows the company sits outside the thesis.
    THESIS_MISMATCH_PASS = "thesis_mismatch_pass"  # noqa: S105 - an investment call
    #: Scored, evidenced, and not close enough to the thesis to spend partner time on.
    LOW_FIT_PASS = "low_fit_pass"  # noqa: S105 - an investment call
    #: Watch because the research fell short, not because the company did.
    INSUFFICIENT_EVIDENCE_WATCH = "insufficient_evidence_watch"
    #: The score reached a higher band and a guardrail held it back.
    GUARDRAIL_CAPPED_WATCH = "guardrail_capped_watch"
    #: Watch on the score band alone.
    BANDED_WATCH = "banded_watch"


_INSUFFICIENT = frozenset({Guardrail.ZERO_CLAIM_DOSSIER, Guardrail.INSUFFICIENT_EVIDENCE})


def call_kind(analysis: StartupAnalysis, recommendation: RecommendationResult) -> CallKind:
    """Classify the call from the artifacts alone."""
    if recommendation.decision is Recommendation.TAKE_A_MEETING:
        return CallKind.EVIDENCE_BACKED_MEETING
    if recommendation.decision is Recommendation.WATCH:
        if _INSUFFICIENT & set(recommendation.guardrails_applied):
            return CallKind.INSUFFICIENT_EVIDENCE_WATCH
        if recommendation.capped:
            return CallKind.GUARDRAIL_CAPPED_WATCH
        return CallKind.BANDED_WATCH
    if analysis.thesis_assessment.is_supported_mismatch:
        return CallKind.THESIS_MISMATCH_PASS
    return CallKind.LOW_FIT_PASS


def call_sentence(
    kind: CallKind,
    *,
    name: str,
    analysis: StartupAnalysis,
    recommendation: RecommendationResult,
    dossier: EvidenceDossier,
) -> str:
    """One sentence, assembled from the decision, the thesis fit, the score and confidence.

    ``name`` arrives already neutralised; everything else is a number or a closed
    vocabulary, so the result carries no untrusted structure.
    """
    score = analysis.total_score
    level = recommendation.confidence.level.value
    fit = analysis.thesis_assessment.verdict.value
    unassessable = len(analysis.components_with_status(AssessmentStatus.NOT_ASSESSABLE))

    if kind is CallKind.EVIDENCE_BACKED_MEETING:
        return (
            f"Take a meeting: {name} scored {score}/100 on evidence that reached the "
            f"take-a-meeting band at {TAKE_A_MEETING_AT}, research confidence is {level}, "
            "and every meeting requirement the policy checks was satisfied."
        )
    if kind is CallKind.THESIS_MISMATCH_PASS:
        return (
            f"Pass: the evidence positively places {name} outside the thesis (recorded fit "
            f"{fit}), and the rubric scored {score}/100 on {level}-confidence research."
        )
    if kind is CallKind.LOW_FIT_PASS:
        return (
            f"Pass: {name} scored {score}/100 against the thesis rubric on {level}-confidence "
            f"research, with thesis fit recorded as {fit} - short of what a meeting needs."
        )
    if kind is CallKind.INSUFFICIENT_EVIDENCE_WATCH:
        if not dossier.claims:
            return (
                f"Watch on insufficient evidence, not on merit: no evidence claim could be "
                f"extracted for {name}, so {score}/100 records unresolved uncertainty rather "
                "than an assessment of the company."
            )
        return (
            f"Watch on insufficient evidence, not on merit: {unassessable} of {len(RUBRIC)} "
            f"rubric dimensions could not be assessed and research confidence is {level}, so "
            f"{name}'s {score}/100 is a statement about the research."
        )
    if kind is CallKind.GUARDRAIL_CAPPED_WATCH:
        applied = len(recommendation.guardrails_applied)
        return (
            f"Watch: {name} scored {score}/100, which lands in the "
            f"{recommendation.band_label} band, but {applied} policy guardrail(s) held the "
            f"call at watch on {level}-confidence research."
        )
    return (
        f"Watch: {name} scored {score}/100, inside the {WATCH_AT}-{TAKE_A_MEETING_AT - 1} "
        f"band, on {level}-confidence research."
    )


def reading(kind: CallKind) -> str:
    """The one line that stops a reader misreading the call."""
    return {
        CallKind.EVIDENCE_BACKED_MEETING: (
            "This is a meeting on evidence-backed scoring that also cleared every "
            "requirement the policy places on a meeting."
        ),
        CallKind.THESIS_MISMATCH_PASS: (
            "This is a pass on evidence, not on absence: the sources show the company sits "
            "outside the thesis."
        ),
        CallKind.LOW_FIT_PASS: (
            "This is a pass on thesis fit at the score the evidence supported. It is not a "
            "finding that the company is failing."
        ),
        CallKind.INSUFFICIENT_EVIDENCE_WATCH: (
            "This is a watch on insufficient evidence. Missing evidence caps how certain "
            "this call can be; it is not evidence of weakness."
        ),
        CallKind.GUARDRAIL_CAPPED_WATCH: (
            "This is a watch by guardrail: the score alone would have gone higher, and the "
            "policy deliberately held it back."
        ),
        CallKind.BANDED_WATCH: "This is a watch on the score band alone; no guardrail fired.",
    }[kind]


#: The same classification, compressed to a ranking-table cell. Kept beside the long
#: wording so the two can never drift into saying different things about one call.
SHORT_RATIONALE: dict[CallKind, str] = {
    CallKind.EVIDENCE_BACKED_MEETING: "Evidence-backed score cleared every meeting requirement",
    CallKind.THESIS_MISMATCH_PASS: "Evidence places it outside the thesis",
    CallKind.LOW_FIT_PASS: "Scored below the watch band on the evidence available",
    CallKind.INSUFFICIENT_EVIDENCE_WATCH: "Insufficient evidence, not a judgement of the company",
    CallKind.GUARDRAIL_CAPPED_WATCH: "Score reached a higher band; a guardrail held it at watch",
    CallKind.BANDED_WATCH: "Inside the watch band on score alone",
}


#: The same guardrails as a table chip. A chip has room for a reason, not a sentence, and
#: "Guardrail" on its own tells a reader nothing they can act on.
GUARDRAIL_CHIPS: dict[str, str] = {
    Guardrail.ZERO_CLAIM_DOSSIER: "No usable evidence",
    Guardrail.INSUFFICIENT_EVIDENCE: "Evidence shortfall",
    Guardrail.IDENTITY_MISMATCH_CAP: "Identity unconfirmed",
    Guardrail.MEETING_NEEDS_CONFIDENCE: "Confidence too low",
    Guardrail.MEETING_NEEDS_PRODUCT: "No product identified",
    Guardrail.MEETING_NEEDS_BUYER: "No buyer identified",
    Guardrail.MEETING_NEEDS_BREADTH: "Evidence too narrow",
}

#: What a badge says beyond the decision word. A bare "Watch" is ambiguous when every watch
#: in a run is an evidence shortfall - the reader cannot tell "promising but unproven" from
#: "we found nothing". The qualifier is display only: the persisted decision is unchanged,
#: and the policy never sees this.
_WATCH_QUALIFIERS: dict[str, str] = {
    Guardrail.ZERO_CLAIM_DOSSIER: "no usable evidence",
    Guardrail.INSUFFICIENT_EVIDENCE: "needs research",
}


def call_label(kind: CallKind, recommendation: RecommendationResult) -> tuple[str, str | None]:
    """The decision word, and the qualifier that disambiguates it.

    Returns ``(decision, qualifier | None)``. The caller decides how to join them; the
    split exists so a template can style the qualifier as secondary.
    """
    decision = DECISION_LABELS[recommendation.decision]
    if recommendation.decision is Recommendation.WATCH:
        for guardrail in recommendation.guardrails_applied:
            if qualifier := _WATCH_QUALIFIERS.get(guardrail):
                return decision, qualifier
        return decision, None
    if kind is CallKind.THESIS_MISMATCH_PASS:
        return decision, "outside thesis"
    return decision, None
