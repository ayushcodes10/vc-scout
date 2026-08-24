"""The memo view model.

Everything a memo displays is computed here, from validated artifacts, and handed to the
template as finished strings. The template places them; it does not decide anything. That
split is deliberate: a template that can branch on a score is a second scoring policy, and
one of them will eventually disagree with the other.

Untrusted text - company names, page titles, model narrative, page excerpts - is
neutralised on the way in by :mod:`vc_scout.render.markdown`. Renderer-authored text, the
policy's own rationale above all, is passed through verbatim so the memo states the
recommendation exactly as the artifact records it.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace

from vc_scout.models.analysis import ScoreComponent, StartupAnalysis, ceiling_for
from vc_scout.models.candidate import Candidate
from vc_scout.models.enums import AssessmentStatus, ThesisFit
from vc_scout.models.evidence import EvidenceDossier
from vc_scout.models.page import PageBundle
from vc_scout.models.recommendation import RecommendationResult
from vc_scout.models.source import SourceReference
from vc_scout.policy import TAKE_A_MEETING_AT
from vc_scout.render import markdown as md
from vc_scout.render.call import (
    DECISION_LABELS,
    GUARDRAIL_LABELS,
    call_kind,
    call_sentence,
    reading,
)
from vc_scout.render.engine import render_template
from vc_scout.render.sources import UNAVAILABLE, SourceEntry, SourceIndex, build_source_index
from vc_scout.rubric import MAX_TOTAL_SCORE, RUBRIC, RUBRIC_VERSION

__all__ = [
    "FIT_LABELS",
    "MAX_MEMO_WORDS",
    "STATUS_LABELS",
    "memo_word_count",
    "render_memo",
    "MemoView",
    "ScoreRow",
    "SourceRow",
    "Statement",
    "build_memo_view",
]

#: The memo is a 60-second read. This is the ceiling the templates are budgeted against,
#: and a test measures every rendered fixture memo against it.
MAX_MEMO_WORDS = 900

#: Per-item word budgets. Long model prose is truncated rather than dropped: a reader
#: needs the finding, and the full text is in the analysis artifact.
_RATIONALE_WORDS = 15
_RISK_WORDS = 26
_THESIS_WORDS = 30
_QUESTION_WORDS = 18
_SECTION_WORDS = 60
_PRODUCT_WORDS = 30

#: How many items of each kind survive into the risks section, and how many in total. The
#: analysis artifact keeps everything; the memo keeps what fits a 60-second read.
_MAX_RISKS = 4
_MAX_CONFLICTS = 2
_MAX_QUESTIONS = 3
_MAX_RISK_ITEMS = 6

STATUS_LABELS: dict[AssessmentStatus, str] = {
    AssessmentStatus.SUPPORTED: "Supported",
    AssessmentStatus.PARTIALLY_SUPPORTED: "Partially supported",
    AssessmentStatus.CONTRADICTED: "Contradicted",
    AssessmentStatus.NOT_ASSESSABLE: "Not assessable",
}

FIT_LABELS: dict[ThesisFit, str] = {
    ThesisFit.ALIGNED: "Aligned with the thesis",
    ThesisFit.ADJACENT: "Adjacent to the thesis",
    ThesisFit.MISMATCH: "Outside the thesis, on evidence",
    ThesisFit.UNDETERMINED: "Not established by the sources",
}

#: What a memo says where an artifact recorded nothing. Never "none" or "n/a", which read
#: as findings about the company rather than as gaps in the research.
NOT_ESTABLISHED = "Not established by the sources"

OPEN_QUESTION = "Open question"


@dataclass(frozen=True, slots=True)
class Statement:
    """One rendered assertion and what it rests on."""

    text: str
    markers: str = ""
    open_question: bool = False

    @property
    def attribution(self) -> str:
        if self.markers:
            return self.markers
        return f"_{OPEN_QUESTION}_" if self.open_question else ""

    @property
    def line(self) -> str:
        """Text and attribution as one line.

        Templates place this rather than composing it, so no template contains an inline
        conditional - Jinja's ``trim_blocks`` eats the newline after one, and a memo whose
        list items silently ran together is exactly the kind of defect a renderer should
        make impossible rather than remember.
        """
        return f"{self.text} {self.attribution}".strip()


@dataclass(frozen=True, slots=True)
class ScoreRow:
    """One row of the scorecard."""

    dimension: str
    score: str
    status: str
    rationale: str
    sources: str


@dataclass(frozen=True, slots=True)
class SnapshotRow:
    label: str
    value: str


@dataclass(frozen=True, slots=True)
class SourceRow:
    """One entry of the memo's numbered source list."""

    marker: str
    title: str
    role: str
    url: str
    observed: str
    excerpt: str
    resolved: bool = True


@dataclass(frozen=True, slots=True)
class MemoView:
    """Everything ``memo.md.j2`` renders, already safe and already decided."""

    company_id: str
    name: str
    badge: str
    score: int
    maximum_score: int
    maximum_achievable: int
    confidence_label: str
    call_sentence: str
    reading: str
    snapshot: list[SnapshotRow]
    rationale: list[str]
    guardrails: list[str]
    thesis_line: Statement
    team: Statement
    product: Statement
    market: Statement
    score_rows: list[ScoreRow]
    total_row: ScoreRow
    risks: list[Statement]
    changers: list[str]
    sources: list[SourceRow]
    generation_note: str
    warnings: list[str] = field(default_factory=list)


def _markers(index: SourceIndex, dossier: EvidenceDossier, claim_ids: list[str]) -> list[str]:
    """Markers for the sources behind ``claim_ids``, in claim then citation order."""
    claims = dossier.claim_index()
    source_ids: list[str] = []
    for claim_id in claim_ids:
        claim = claims.get(claim_id)
        if claim is None:
            # The analysis validator resolves every reference before an analysis is
            # written, so this is unreachable through the pipeline. If an artifact is ever
            # hand-edited, the memo says so rather than rendering a bare assertion.
            index.warnings.append(
                f"{index.company_id}: analysis cites evidence {claim_id}, which its dossier "
                "does not contain; the statement is rendered without that citation"
            )
            continue
        source_ids.extend(claim.source_ids)
    return index.markers_for(source_ids)


def _statement(
    text: str,
    *,
    index: SourceIndex,
    dossier: EvidenceDossier,
    claim_ids: list[str],
    unknown_refs: list[str],
    limit: int | None = None,
) -> Statement:
    """A neutralised statement carrying either source markers or an open-question label."""
    body = md.text(md.truncate_words(text, limit) if limit else text)
    markers = _markers(index, dossier, claim_ids)
    return Statement(
        text=body,
        markers=md.join_markers(markers),
        open_question=not markers and bool(unknown_refs),
    )


def _snapshot(
    *,
    candidate: Candidate | None,
    analysis: StartupAnalysis,
    recommendation: RecommendationResult,
    index: SourceIndex,
    headroom: int,
) -> list[SnapshotRow]:
    rows = [
        SnapshotRow(
            "Website",
            md.autolink(candidate.website) if candidate and candidate.website else NOT_ESTABLISHED,
        ),
    ]
    discovery = md.join_markers(index.markers_for(list(candidate.source_ids))) if candidate else ""
    if discovery:
        rows.append(SnapshotRow("Discovered via", discovery))
    rows += [
        SnapshotRow(
            "Product in plain language",
            md.text(md.truncate_words(analysis.plain_language_product, _PRODUCT_WORDS)),
        ),
        SnapshotRow("Buyer", md.text(analysis.buyer, empty=NOT_ESTABLISHED)),
        SnapshotRow("Workflow", md.text(analysis.workflow, empty=NOT_ESTABLISHED)),
        SnapshotRow("Thesis fit", FIT_LABELS[analysis.thesis_assessment.verdict]),
        SnapshotRow("Final recommendation", DECISION_LABELS[recommendation.decision]),
        SnapshotRow(
            "Model suggestion",
            DECISION_LABELS[recommendation.model_suggested]
            if recommendation.model_suggested
            else "No suggestion",
        ),
    ]
    if recommendation.model_disagreed:
        suggested = DECISION_LABELS[recommendation.model_suggested]  # type: ignore[index]
        rows.append(
            SnapshotRow(
                "Policy/model disagreement",
                f"The analysis model suggested {suggested.lower()}; the deterministic policy "
                f"decided {DECISION_LABELS[recommendation.decision].lower()}. The policy is "
                "binding.",
            )
        )
    confidence = recommendation.confidence
    rows += [
        SnapshotRow(
            "Research confidence", f"{confidence.level.value} ({confidence.score:.2f} of 1.00)"
        ),
        SnapshotRow(
            "Maximum achievable score",
            f"{headroom}/{MAX_TOTAL_SCORE} under the recorded assessment statuses"
            + (
                ""
                if headroom >= TAKE_A_MEETING_AT
                else f" - the take-a-meeting band at {TAKE_A_MEETING_AT} was out of reach on "
                "this evidence"
            ),
        ),
    ]
    return rows


def _score_row(
    component: ScoreComponent, *, index: SourceIndex, dossier: EvidenceDossier
) -> ScoreRow:
    spec = next(spec for spec in RUBRIC if spec.key is component.component)
    markers = md.join_markers(_markers(index, dossier, component.evidence_claim_ids))
    if markers:
        sources = markers
    elif component.unknown_references:
        sources = f"_{OPEN_QUESTION}_"
    else:
        sources = "-"
    return ScoreRow(
        dimension=spec.title,
        score=f"{component.score} / {component.maximum}",
        status=STATUS_LABELS[component.assessment_status],
        rationale=md.text(md.truncate_words(component.rationale, _RATIONALE_WORDS)),
        sources=sources,
    )


def _risks(
    analysis: StartupAnalysis, dossier: EvidenceDossier, index: SourceIndex
) -> list[Statement]:
    """Risks, conflicts, open questions and warnings, deduplicated, in that order."""
    items: list[Statement] = []
    seen: set[str] = set()

    def add(statement: Statement) -> None:
        key = statement.text.casefold()
        if key and key not in seen:
            seen.add(key)
            items.append(statement)

    for risk in analysis.risks[:_MAX_RISKS]:
        add(
            _statement(
                risk.text,
                index=index,
                dossier=dossier,
                claim_ids=risk.evidence_claim_ids,
                unknown_refs=risk.unknown_references,
                limit=_RISK_WORDS,
            )
        )
    for conflict in dossier.conflicts[:_MAX_CONFLICTS]:
        add(
            Statement(
                text="Sources disagree: "
                + md.text(md.truncate_words(conflict.summary, _RISK_WORDS)),
                markers=md.join_markers(index.markers_for(list(conflict.source_ids))),
            )
        )
    for question in analysis.open_questions[:_MAX_QUESTIONS]:
        add(
            Statement(
                text=md.text(md.truncate_words(question, _QUESTION_WORDS)), open_question=True
            )
        )
    for unknown in dossier.unknowns[:_MAX_QUESTIONS]:
        add(
            Statement(
                text=md.text(md.truncate_words(unknown.question, _QUESTION_WORDS)),
                open_question=True,
            )
        )
    for warning in analysis.identity_warnings:
        add(Statement(text="Identity warning: " + md.text(warning)))
    for warning in analysis.analysis_warnings:
        add(Statement(text="Analysis note: " + md.text(warning)))
    return items[:_MAX_RISK_ITEMS]


def _guardrail_lines(recommendation: RecommendationResult, index: SourceIndex) -> list[str]:
    lines: list[str] = []
    for guardrail in recommendation.guardrails_applied:
        label = GUARDRAIL_LABELS.get(guardrail)
        if label is None:
            # A guardrail the policy added and this renderer has not been taught to phrase.
            # Better to show its identifier and flag it than to describe it wrongly.
            index.warnings.append(
                f"{index.company_id}: guardrail {guardrail!r} has no reader-facing wording; "
                "the memo shows its policy identifier"
            )
            label = f"Policy guardrail `{md.text(guardrail)}` was applied."
        lines.append(label)
    return lines


def _source_row(marker: str, entry: SourceEntry) -> SourceRow:
    """One source-list entry, including the ones nothing in the run could describe.

    An unresolvable source keeps its marker and shows its internal identifier. Dropping it
    would silently unanchor whatever cited it, which is the one outcome a citation chain
    must not allow.
    """
    if not entry.resolved:
        return SourceRow(
            marker=marker,
            title=UNAVAILABLE,
            role="no title or URL was recorded by any artifact in this run",
            url=f"internal identifier `{md.text(entry.source_id)}`",
            observed="date not recorded",
            excerpt="",
            resolved=False,
        )
    return SourceRow(
        marker=marker,
        title=md.text(entry.title, empty="Untitled page"),
        role=entry.role_label,
        url=md.autolink(entry.url) if entry.url else "no URL recorded",
        observed=entry.observed_on or "date not recorded",
        excerpt=md.text(entry.excerpt),
        resolved=True,
    )


def _generation_note(
    analysis: StartupAnalysis, dossier: EvidenceDossier, recommendation: RecommendationResult
) -> str:
    """One factual line of AI-workflow provenance. Not a disclaimer, and not marketing."""
    unrecorded = "unrecorded"
    return (
        f"Evidence extracted by {dossier.provider or unrecorded}/{dossier.model or unrecorded} "
        f"({dossier.prompt_version or unrecorded}); analysed by "
        f"{analysis.provider}/{analysis.model} ({analysis.prompt_version}). "
        f"Thesis {analysis.thesis_version}, rubric {RUBRIC_VERSION}, policy "
        f"{recommendation.policy_version}. The total was recomputed in Python and the "
        "recommendation was made by the deterministic policy, not by the model."
    )


def build_memo_view(
    *,
    candidate: Candidate | None,
    candidate_sources: dict[str, SourceReference],
    pages: PageBundle | None,
    dossier: EvidenceDossier,
    analysis: StartupAnalysis,
    recommendation: RecommendationResult,
) -> MemoView:
    """Assemble the memo for one company from its own validated artifacts."""
    index = build_source_index(
        analysis.company_id,
        candidate=candidate,
        candidate_sources=candidate_sources,
        pages=pages,
        dossier=dossier,
    )
    name = md.text(candidate.name if candidate else analysis.company_id, empty=analysis.company_id)
    kind = call_kind(analysis, recommendation)
    headroom = sum(
        ceiling_for(component.component, component.assessment_status)
        for component in analysis.score_components
    )

    # Markers are assigned in the order they are first asked for, so these are built in
    # the order the memo reads: snapshot, why-this-call, investment view, scorecard, risks.
    # Reordering them renumbers every citation in the document.
    snapshot = _snapshot(
        candidate=candidate,
        analysis=analysis,
        recommendation=recommendation,
        index=index,
        headroom=headroom,
    )
    guardrails = _guardrail_lines(recommendation, index)
    thesis_line = _statement(
        analysis.thesis_assessment.rationale,
        index=index,
        dossier=dossier,
        claim_ids=analysis.thesis_assessment.evidence_claim_ids,
        unknown_refs=analysis.thesis_assessment.unknown_references,
        limit=_THESIS_WORDS,
    )
    sections = [
        _statement(
            section.text,
            limit=_SECTION_WORDS,
            index=index,
            dossier=dossier,
            claim_ids=section.evidence_claim_ids,
            unknown_refs=section.unknown_references,
        )
        for section in (
            analysis.team_assessment,
            analysis.product_assessment,
            analysis.market_assessment,
        )
    ]
    components = analysis.component_index()
    score_rows = [_score_row(components[spec.key], index=index, dossier=dossier) for spec in RUBRIC]
    risks = _risks(analysis, dossier, index)
    total_row = ScoreRow(
        dimension="**Total**",
        score=f"**{analysis.total_score} / {MAX_TOTAL_SCORE}**",
        # With nothing assessable there is no denominator worth quoting, and "0 of 100
        # points were assessable" beside a positive total reads as nonsense.
        status=(
            "No dimension was assessable"
            if analysis.scored_out_of == 0
            else f"{analysis.scored_out_of} of {MAX_TOTAL_SCORE} points were assessable"
        ),
        rationale=f"Maximum achievable under these statuses: {headroom}/{MAX_TOTAL_SCORE}.",
        sources="",
    )

    view = MemoView(
        company_id=analysis.company_id,
        name=name,
        badge=DECISION_LABELS[recommendation.decision],
        score=analysis.total_score,
        maximum_score=MAX_TOTAL_SCORE,
        maximum_achievable=headroom,
        confidence_label=recommendation.confidence.level.value,
        call_sentence=call_sentence(
            kind,
            name=name,
            analysis=analysis,
            recommendation=recommendation,
            dossier=dossier,
        ),
        reading=reading(kind),
        snapshot=snapshot,
        rationale=[md.verbatim(line) for line in recommendation.rationale],
        guardrails=guardrails,
        thesis_line=thesis_line,
        team=sections[0],
        product=sections[1],
        market=sections[2],
        score_rows=score_rows,
        total_row=total_row,
        risks=risks,
        changers=[md.text(changer) for changer in analysis.recommendation_changers],
        sources=[],
        generation_note=_generation_note(analysis, dossier, recommendation),
        warnings=[],
    )
    # Sources last: markers are assigned as the sections above ask for them, so the list
    # is exactly what the memo cites, numbered in reading order.
    sources = [_source_row(marker, entry) for marker, entry in index.cited()]
    return replace(view, sources=sources, warnings=list(index.warnings))


def render_memo(view: MemoView) -> str:
    """Render one memo. The template places what this module already decided."""
    return render_template("memo.md.j2", view=view)


#: Tokens that are table or list scaffolding rather than words a partner reads.
_SCAFFOLDING = frozenset({"|", "-", "--", "---", "---:", ":---", "#", "##", "###", ">", "·"})


def memo_word_count(markdown: str) -> int:
    """Words a reader actually reads, excluding Markdown scaffolding.

    Counting raw whitespace-separated tokens would score every table pipe as a word, which
    inflates a scorecard-heavy memo by a couple of hundred "words" that nobody reads. This
    counts the prose, which is what the 60-second budget is really about.
    """
    return sum(1 for token in markdown.split() if token.strip("*_`") not in _SCAFFOLDING)
