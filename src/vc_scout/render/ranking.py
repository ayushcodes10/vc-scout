"""The portfolio ranking view model.

``ranking.md`` is the reviewer's entry point, so it has to carry its own context: the
thesis being applied, the rubric and thresholds that produced the numbers, and an explicit
statement of what a low score does and does not mean.

The ordering is triage order - the sequence a partner should work the list in - and the
document says so. It is deliberately not a quality ranking. A watch that exists only
because the research came up short is not evidence that the company is better than one
that was scored on real evidence and passed; sorting cannot express that distinction, so
the prose does.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from vc_scout.models.analysis import StartupAnalysis, ceiling_for
from vc_scout.models.candidate import Candidate
from vc_scout.models.enums import AssessmentStatus, ConfidenceLevel, Recommendation
from vc_scout.models.evidence import EvidenceDossier
from vc_scout.models.recommendation import RecommendationResult
from vc_scout.policy import CONFIDENCE_HIGH_AT, CONFIDENCE_MEDIUM_AT, TAKE_A_MEETING_AT, WATCH_AT
from vc_scout.render import markdown as md
from vc_scout.render.call import DECISION_LABELS, GUARDRAIL_LABELS, SHORT_RATIONALE, call_kind
from vc_scout.render.engine import render_template
from vc_scout.render.memo import FIT_LABELS, STATUS_LABELS
from vc_scout.rubric import MAX_TOTAL_SCORE, RUBRIC, RUBRIC_VERSION
from vc_scout.thesis import THESIS_TEXT, THESIS_VERSION

__all__ = ["RankingRow", "RankingView", "build_ranking_view", "render_ranking", "sort_key"]

#: Triage order: what a partner should look at first. Not a quality ordering.
_DECISION_ORDER: dict[Recommendation, int] = {
    Recommendation.TAKE_A_MEETING: 0,
    Recommendation.WATCH: 1,
    Recommendation.PASS: 2,
}

_CONFIDENCE_ORDER: dict[ConfidenceLevel, int] = {
    ConfidenceLevel.HIGH: 0,
    ConfidenceLevel.MEDIUM: 1,
    ConfidenceLevel.LOW: 2,
}


@dataclass(frozen=True, slots=True)
class RankingRow:
    """One row of the ranking table, already rendered."""

    rank: int
    company_id: str
    company: str
    call: str
    score: str
    confidence: str
    thesis_fit: str
    maximum_achievable: str
    rationale: str
    memo_link: str


@dataclass(frozen=True, slots=True)
class RankingView:
    """Everything ``ranking.md.j2`` renders."""

    run_id: str
    thesis: str
    thesis_version: str
    rubric_rows: list[tuple[str, str, str]]
    thresholds: list[str]
    candidate_count: int
    memo_count: int
    recommendation_counts: list[tuple[str, int]]
    evidence_note: list[str]
    rows: list[RankingRow]
    guardrail_lines: list[str]
    disagreement_lines: list[str]
    no_meeting: list[str]
    missing: list[str]
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class RankingInput:
    """One analysed candidate, as the ranking needs it."""

    candidate: Candidate | None
    analysis: StartupAnalysis
    recommendation: RecommendationResult
    dossier: EvidenceDossier
    memo_relative_path: str


def sort_key(item: RankingInput) -> tuple[int, int, int, str, str]:
    """Triage order: recommendation, then score, then confidence, then name.

    Company ID is the final tiebreak so the order is total - two candidates with the same
    display name must still sort identically on every machine.
    """
    name = (item.candidate.name if item.candidate else item.analysis.company_id).casefold()
    return (
        _DECISION_ORDER[item.recommendation.decision],
        -item.analysis.total_score,
        _CONFIDENCE_ORDER[item.recommendation.confidence.level],
        name,
        item.analysis.company_id,
    )


def _headroom(analysis: StartupAnalysis) -> int:
    return sum(
        ceiling_for(component.component, component.assessment_status)
        for component in analysis.score_components
    )


def build_ranking_view(
    run_id: str,
    items: list[RankingInput],
    *,
    candidate_count: int,
    missing: list[str],
) -> RankingView:
    """Assemble the ranking from every candidate that produced a memo."""
    ordered = sorted(items, key=sort_key)
    decisions: Counter[Recommendation] = Counter(item.recommendation.decision for item in ordered)
    statuses: Counter[AssessmentStatus] = Counter(
        component.assessment_status
        for item in ordered
        for component in item.analysis.score_components
    )
    unreachable = [item for item in ordered if _headroom(item.analysis) < TAKE_A_MEETING_AT]

    rows: list[RankingRow] = []
    guardrails: Counter[str] = Counter()
    disagreements: list[str] = []
    for rank, item in enumerate(ordered, start=1):
        name = md.text(
            item.candidate.name if item.candidate else item.analysis.company_id,
            empty=item.analysis.company_id,
        )
        kind = call_kind(item.analysis, item.recommendation)
        rows.append(
            RankingRow(
                rank=rank,
                company_id=item.analysis.company_id,
                company=name,
                call=DECISION_LABELS[item.recommendation.decision],
                score=f"{item.analysis.total_score}/{MAX_TOTAL_SCORE}",
                confidence=item.recommendation.confidence.level.value,
                thesis_fit=FIT_LABELS[item.analysis.thesis_assessment.verdict],
                maximum_achievable=f"{_headroom(item.analysis)}/{MAX_TOTAL_SCORE}",
                rationale=SHORT_RATIONALE[kind],
                memo_link=md.internal_link(name, item.memo_relative_path),
            )
        )
        guardrails.update(item.recommendation.guardrails_applied)
        if item.recommendation.model_disagreed:
            suggested = DECISION_LABELS[item.recommendation.model_suggested]  # type: ignore[index]
            decided = DECISION_LABELS[item.recommendation.decision]
            disagreements.append(
                f"**{name}** - the analysis model suggested {suggested.lower()}; the policy "
                f"decided {decided.lower()}."
            )

    guardrail_lines = [
        f"**{count}x** {GUARDRAIL_LABELS.get(name, f'Policy guardrail `{md.text(name)}`.')}"
        for name, count in sorted(guardrails.items())
    ]

    return RankingView(
        run_id=run_id,
        thesis=md.verbatim(THESIS_TEXT),
        thesis_version=THESIS_VERSION,
        rubric_rows=[
            (spec.title, str(spec.max_points), md.verbatim(spec.description)) for spec in RUBRIC
        ],
        thresholds=[
            f"**Take a meeting** at {TAKE_A_MEETING_AT}/{MAX_TOTAL_SCORE} and above, "
            f"**watch** from {WATCH_AT} to {TAKE_A_MEETING_AT - 1}, **pass** below {WATCH_AT}.",
            f"Research confidence is **high** at {CONFIDENCE_HIGH_AT:.2f} and above, "
            f"**medium** from {CONFIDENCE_MEDIUM_AT:.2f}, **low** below that. It measures how "
            "much the research established, never how good the company is.",
            "A dimension's assessment status caps what it may score: supported 100%, "
            "partially supported 70%, not assessable 50%, contradicted 100%.",
            f"Rubric {RUBRIC_VERSION}, thesis {THESIS_VERSION}.",
        ],
        candidate_count=candidate_count,
        memo_count=len(ordered),
        recommendation_counts=[
            (DECISION_LABELS[decision], decisions.get(decision, 0))
            for decision in (
                Recommendation.TAKE_A_MEETING,
                Recommendation.WATCH,
                Recommendation.PASS,
            )
        ],
        evidence_note=[
            "A low score here means the evidence available did not support a higher one. It "
            "is not a finding that the company is weak. Where a dimension could not be "
            "assessed at all, the memo says so rather than scoring it as a failure.",
            "The ordering below is **triage order** - which memo to open first - and not a "
            "quality ranking. A watch that exists only because the research came up short is "
            "not a judgement that the company is better than one that was passed on evidence.",
        ],
        rows=rows,
        guardrail_lines=guardrail_lines,
        disagreement_lines=disagreements,
        no_meeting=_no_meeting_facts(ordered, statuses, unreachable)
        if not decisions.get(Recommendation.TAKE_A_MEETING)
        else [],
        missing=missing,
    )


def _no_meeting_facts(
    ordered: list[RankingInput],
    statuses: Counter[AssessmentStatus],
    unreachable: list[RankingInput],
) -> list[str]:
    """Why the run produced no meeting, from the run's own counts.

    Deliberately arithmetic. No candidate is talked up to fill the band, and no explanation
    is offered beyond what the recorded assessment statuses actually imply.
    """
    slots = sum(statuses.values())
    breakdown = ", ".join(
        f"{statuses.get(status, 0)} {STATUS_LABELS[status].lower()}"
        for status in (
            AssessmentStatus.SUPPORTED,
            AssessmentStatus.PARTIALLY_SUPPORTED,
            AssessmentStatus.CONTRADICTED,
            AssessmentStatus.NOT_ASSESSABLE,
        )
    )
    best = max((_headroom(item.analysis) for item in ordered), default=0)
    facts = [
        f"No candidate in this run reached the take-a-meeting band at "
        f"{TAKE_A_MEETING_AT}/{MAX_TOTAL_SCORE}.",
        f"Across {len(ordered)} analysed candidate(s), the {slots} scored dimension slots "
        f"were assessed as: {breakdown}.",
    ]
    if unreachable:
        facts.append(
            f"For {len(unreachable)} of {len(ordered)} candidate(s), the assessment statuses "
            f"recorded capped the achievable total below {TAKE_A_MEETING_AT} before any "
            f"judgement about the company; the highest achievable total in this run was "
            f"{best}/{MAX_TOTAL_SCORE}."
        )
    facts.append(
        "That is a statement about the evidence this run could gather, not a conclusion "
        "that these companies are uninvestable. No score has been raised to produce a "
        "meeting."
    )
    return facts


def render_ranking(view: RankingView) -> str:
    """Render ``ranking.md``."""
    return render_template("ranking.md.j2", view=view)
