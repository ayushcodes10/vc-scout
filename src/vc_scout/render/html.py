"""The static site's view models and its own Jinja environment.

Two things separate this module from :mod:`vc_scout.render.memo`, which renders the same
artifacts as Markdown.

**Escaping runs the other way.** A Markdown memo needs Markdown-aware escaping applied
before Jinja sees a value, so its environment has autoescape off. HTML has a correct,
context-aware escaper built into Jinja, so this environment has autoescape **on** and view
models carry raw text. Nothing here is ever marked safe: if a value needs markup, the
template supplies the markup and the value stays text.

**Links are validated, not trusted.** Every ``href`` the site emits passes through
:func:`safe_href` or :func:`internal_href` first. A URL that is not absolute ``http``/
``https``, or a relative path that is not one this generator built, renders as inert text
rather than as a link.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined
from markupsafe import Markup

from vc_scout.models.analysis import ScoreComponent, StartupAnalysis, ceiling_for
from vc_scout.models.candidate import Candidate
from vc_scout.models.enums import Recommendation
from vc_scout.models.evidence import EvidenceDossier
from vc_scout.models.page import PageBundle
from vc_scout.models.recommendation import RecommendationResult
from vc_scout.models.source import SourceReference, is_safe_url
from vc_scout.policy import (
    CONFIDENCE_HIGH_AT,
    CONFIDENCE_MEDIUM_AT,
    TAKE_A_MEETING_AT,
    WATCH_AT,
)
from vc_scout.render.call import (
    DECISION_LABELS,
    GUARDRAIL_CHIPS,
    GUARDRAIL_LABELS,
    call_kind,
    call_label,
    call_sentence,
    reading,
)
from vc_scout.render.memo import FIT_LABELS, STATUS_LABELS
from vc_scout.render.sources import UNAVAILABLE, SourceIndex, build_source_index
from vc_scout.rubric import MAX_TOTAL_SCORE, RUBRIC, RUBRIC_VERSION
from vc_scout.thesis import THESIS_TEXT, THESIS_VERSION

__all__ = [
    "NOT_ESTABLISHED",
    "bar_width",
    "SITE_TEMPLATE_VERSION",
    "CompanyView",
    "IndexView",
    "build_company_view",
    "build_index_view",
    "embed_json",
    "html_environment",
    "internal_href",
    "plain",
    "render_company",
    "render_index",
    "safe_href",
]

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

#: Bumped whenever the generated page shape changes. Recorded in ``ui-report.json``.
SITE_TEMPLATE_VERSION = "site_v1"

#: What the site says where an artifact recorded nothing. Never "none" or "n/a", which read
#: as findings about the company rather than as gaps in the research.
NOT_ESTABLISHED = "Not established by the sources"

#: A relative path this generator built: at most two leading ``../`` steps, then one
#: optional directory, then a filename of safe characters. Anything else is not a link.
_INTERNAL_PATH = re.compile(r"^(?:\.\./){0,2}(?:[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")
_WHITESPACE = re.compile(r"\s+")
_URL_FORBIDDEN = re.compile(r"[\s<>\"'`\\]")

_ENVIRONMENT: Environment | None = None


def html_environment() -> Environment:
    """The site's environment. Autoescape on, undefined values fatal."""
    global _ENVIRONMENT  # noqa: PLW0603 - one process-wide template cache, built once.
    if _ENVIRONMENT is None:
        _ENVIRONMENT = Environment(
            loader=FileSystemLoader(TEMPLATE_DIR),
            autoescape=True,
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
    return _ENVIRONMENT


def plain(value: str | None, *, empty: str = "") -> str:
    """Normalise untrusted text without escaping it.

    Jinja escapes on output, so this only removes what escaping does not address: control
    and format characters, which render invisibly, and bidirectional overrides, which can
    make a line read as something other than its source.
    """
    if value is None:
        return empty
    kept = "".join(
        " " if char.isspace() else char
        for char in value
        if char.isspace() or unicodedata.category(char) not in {"Cc", "Cf"}
    )
    return _WHITESPACE.sub(" ", kept).strip() or empty


def safe_href(url: str | None) -> str | None:
    """``url`` if it is a renderable absolute http(s) link, otherwise ``None``.

    ``None`` is the signal to a template that the value must be shown as text. Returning a
    placeholder string instead would put an unusable value in an ``href``.
    """
    stripped = (url or "").strip()
    if not stripped or not is_safe_url(stripped) or _URL_FORBIDDEN.search(stripped):
        return None
    return stripped


def internal_href(path: str) -> str:
    """Validate a relative path this generator constructed.

    Company IDs are already pattern-checked by the store, so this is a second gate rather
    than the only one - but a link is the one place a bad identifier becomes clickable.
    """
    if not _INTERNAL_PATH.match(path) or ".." in path.replace("../", ""):
        raise ValueError(f"refusing to link to {path!r}")
    return path


def embed_json(payload: object) -> Markup:
    """Serialise ``payload`` for a ``<script type="application/json">`` block.

    ``<``, ``>`` and ``&`` become unicode escapes, so no value can close the element it
    sits in - the classic way embedded data turns into script execution. U+2028 and U+2029
    are escaped too: they are valid JSON but terminate a line to a JavaScript parser.

    This is the one value in the site marked safe, and the reason is that HTML escaping
    here would be *wrong*: a browser does not decode entities inside a ``<script>``
    element, so an autoescaped ``&quot;`` would corrupt the JSON rather than protect it.
    What is marked safe is not an untrusted string - it is the output of a JSON serialiser
    followed by an escape strictly stronger than HTML escaping for this context, and every
    untrusted value inside it has already been JSON-encoded.
    """
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return Markup(  # noqa: S704 - generator-produced JSON; see the docstring.
        raw.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace(" ", "\\u2028")
        .replace(" ", "\\u2029")
    )


def bar_width(percent: int) -> int:
    """A bar width quantised to 5%.

    Widths are CSS classes rather than inline styles, because the site's Content Security
    Policy carries no ``unsafe-inline`` - so a class per step is what a bar can be.
    """
    return max(0, min(100, round(percent / 5) * 5))


# -- view models -------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Link:
    """A label and, when the target validated, somewhere to go."""

    label: str
    href: str | None = None
    external: bool = False


@dataclass(frozen=True, slots=True)
class Statement:
    """One rendered assertion and what it rests on."""

    text: str
    markers: list[str] = field(default_factory=list)
    open_question: bool = False


@dataclass(frozen=True, slots=True)
class Fact:
    """One labelled value, with an optional line explaining what it means."""

    label: str
    value: str
    detail: str | None = None


@dataclass(frozen=True, slots=True)
class ScoreRow:
    """One scorecard row, with the geometry its bar needs."""

    dimension: str
    description: str
    score: int
    maximum: int
    percent: int
    width: int
    status: str
    status_slug: str
    rationale: str
    markers: list[str]
    open_question: bool


@dataclass(frozen=True, slots=True)
class SourceRow:
    marker: str
    title: str
    role: str
    url: str | None
    url_text: str
    observed: str
    excerpt: str
    resolved: bool


@dataclass(frozen=True, slots=True)
class CompanyView:
    """Everything ``company.html.j2`` renders."""

    company_id: str
    name: str
    rank: int
    decision: str
    decision_slug: str
    call_qualifier: str | None
    guardrail_ids: list[str]
    score: int
    maximum_score: int
    score_percent: int
    score_width: int
    maximum_achievable: int
    meeting_reachable: bool
    confidence: str
    confidence_score: float
    thesis_fit: str
    call_sentence: str
    reading: str
    disagreement: str | None
    website: str | None
    memo_href: str
    previous: Link | None
    next: Link | None
    snapshot: list[Fact]
    rationale: list[str]
    guardrails: list[str]
    thesis_line: Statement
    sections: list[tuple[str, Statement]]
    competitive: list[Statement]
    score_rows: list[ScoreRow]
    risks: list[Statement]
    conflicts: list[Statement]
    open_questions: list[Statement]
    identity_warnings: list[str]
    analysis_warnings: list[str]
    changers: list[str]
    sources: list[SourceRow]
    provenance: list[Fact]
    unresolved_sources: int
    page_warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class CandidateRow:
    """One row of the portfolio table, and the record the filters read."""

    rank: int
    company_id: str
    name: str
    decision: str
    decision_slug: str
    call_qualifier: str | None
    score: int
    score_percent: int
    score_width: int
    confidence: str
    confidence_slug: str
    thesis_fit: str
    thesis_slug: str
    product: str
    buyer: str
    workflow: str
    maximum_achievable: int
    meeting_reachable: bool
    #: Short, reader-facing reasons for the Context column - a guardrail's own reason
    #: rather than the word "guardrail", plus a policy override where one happened.
    flags: list[str]
    disagreement: str | None
    detail_href: str
    website: str | None
    search_text: str


@dataclass(frozen=True, slots=True)
class IndexView:
    """Everything ``index.html.j2`` renders."""

    run_id: str
    ranking_href: str
    summary: list[Fact]
    thesis: str
    thesis_version: str
    thresholds: list[str]
    evidence_note: str
    rows: list[CandidateRow]
    filter_data: Markup
    no_meeting: list[str]
    missing: list[str]
    counts: dict[str, int]


# -- builders ----------------------------------------------------------------


def _statement(
    text: str,
    *,
    index: SourceIndex,
    dossier: EvidenceDossier,
    claim_ids: list[str],
    unknown_refs: list[str],
) -> Statement:
    claims = dossier.claim_index()
    source_ids: list[str] = []
    for claim_id in claim_ids:
        claim = claims.get(claim_id)
        if claim is None:
            index.warnings.append(
                f"{index.company_id}: analysis cites evidence {claim_id}, which its dossier "
                "does not contain; the statement is shown without that citation"
            )
            continue
        source_ids.extend(claim.source_ids)
    markers = index.markers_for(source_ids)
    return Statement(
        text=plain(text),
        markers=markers,
        open_question=not markers and bool(unknown_refs),
    )


def _score_row(
    component: ScoreComponent, *, index: SourceIndex, dossier: EvidenceDossier
) -> ScoreRow:
    spec = next(spec for spec in RUBRIC if spec.key is component.component)
    statement = _statement(
        component.rationale,
        index=index,
        dossier=dossier,
        claim_ids=component.evidence_claim_ids,
        unknown_refs=component.unknown_references,
    )
    return ScoreRow(
        dimension=spec.title,
        description=spec.description,
        score=component.score,
        maximum=component.maximum,
        percent=round(100 * component.score / component.maximum),
        width=bar_width(round(100 * component.score / component.maximum)),
        status=STATUS_LABELS[component.assessment_status],
        status_slug=component.assessment_status.value.replace("_", "-"),
        rationale=statement.text,
        markers=statement.markers,
        open_question=statement.open_question,
    )


def _source_rows(index: SourceIndex) -> list[SourceRow]:
    rows: list[SourceRow] = []
    for marker, entry in index.cited():
        href = safe_href(entry.url)
        rows.append(
            SourceRow(
                marker=marker,
                title=plain(entry.title, empty="Untitled page") if entry.resolved else UNAVAILABLE,
                role=entry.role_label
                if entry.resolved
                else "no title or URL was recorded by any artifact in this run",
                url=href,
                url_text=plain(entry.url)
                if entry.url
                else f"internal identifier {entry.source_id}",
                observed=entry.observed_on or "date not recorded",
                excerpt=plain(entry.excerpt),
                resolved=entry.resolved and href is not None,
            )
        )
    return rows


def _disagreement(recommendation: RecommendationResult) -> str | None:
    if not recommendation.model_disagreed or recommendation.model_suggested is None:
        return None
    return (
        f"The analysis model suggested {DECISION_LABELS[recommendation.model_suggested].lower()}; "
        f"the deterministic policy decided {DECISION_LABELS[recommendation.decision].lower()}. "
        "The policy is binding."
    )


def _guardrail_lines(recommendation: RecommendationResult, index: SourceIndex) -> list[str]:
    lines: list[str] = []
    for guardrail in recommendation.guardrails_applied:
        label = GUARDRAIL_LABELS.get(guardrail)
        if label is None:
            index.warnings.append(
                f"{index.company_id}: guardrail {guardrail!r} has no reader-facing wording"
            )
            label = f"Policy guardrail {guardrail} was applied."
        lines.append(label)
    return lines


def headroom_for(analysis: StartupAnalysis) -> int:
    """The highest total these assessment statuses could have reached."""
    return sum(
        ceiling_for(component.component, component.assessment_status)
        for component in analysis.score_components
    )


def build_company_view(
    *,
    candidate: Candidate | None,
    candidate_sources: dict[str, SourceReference],
    pages: PageBundle | None,
    dossier: EvidenceDossier,
    analysis: StartupAnalysis,
    recommendation: RecommendationResult,
    rank: int,
    previous: Link | None,
    next_: Link | None,
) -> CompanyView:
    """Assemble one company page from that company's own validated artifacts."""
    index = build_source_index(
        analysis.company_id,
        candidate=candidate,
        candidate_sources=candidate_sources,
        pages=pages,
        dossier=dossier,
    )
    name = plain(candidate.name if candidate else analysis.company_id, empty=analysis.company_id)
    kind = call_kind(analysis, recommendation)
    headroom = headroom_for(analysis)
    confidence = recommendation.confidence

    # Markers are assigned on first use, so these are built in the order the page reads.
    snapshot = [
        Fact("Product in plain language", plain(analysis.plain_language_product)),
        Fact("Buyer", plain(analysis.buyer, empty=NOT_ESTABLISHED)),
        Fact("Workflow", plain(analysis.workflow, empty=NOT_ESTABLISHED)),
        Fact("Final call", DECISION_LABELS[recommendation.decision]),
        Fact(
            "Model suggestion",
            DECISION_LABELS[recommendation.model_suggested]
            if recommendation.model_suggested
            else "No suggestion",
            "Advisory only. The policy never reads it.",
        ),
        Fact(
            "Maximum achievable score",
            f"{headroom}/{MAX_TOTAL_SCORE}",
            "The highest total these assessment statuses allow.",
        ),
        Fact(
            "Meeting band reachable",
            "Yes" if headroom >= TAKE_A_MEETING_AT else "No",
            f"Whether {TAKE_A_MEETING_AT}/{MAX_TOTAL_SCORE} was arithmetically in reach on "
            "this evidence.",
        ),
    ]
    discovery = index.markers_for(list(candidate.source_ids)) if candidate else []
    if discovery:
        snapshot.insert(
            0, Fact("Discovered via", ", ".join(discovery), "Sources listed at the foot.")
        )

    guardrails = _guardrail_lines(recommendation, index)
    thesis_line = _statement(
        analysis.thesis_assessment.rationale,
        index=index,
        dossier=dossier,
        claim_ids=analysis.thesis_assessment.evidence_claim_ids,
        unknown_refs=analysis.thesis_assessment.unknown_references,
    )
    components = analysis.component_index()
    score_rows = [_score_row(components[spec.key], index=index, dossier=dossier) for spec in RUBRIC]
    sections = [
        (
            title,
            _statement(
                section.text,
                index=index,
                dossier=dossier,
                claim_ids=section.evidence_claim_ids,
                unknown_refs=section.unknown_references,
            ),
        )
        for title, section in (
            ("Team", analysis.team_assessment),
            ("Product", analysis.product_assessment),
            ("Market", analysis.market_assessment),
        )
    ]
    competitive = [
        _statement(
            observation.text,
            index=index,
            dossier=dossier,
            claim_ids=observation.evidence_claim_ids,
            unknown_refs=[],
        )
        for observation in analysis.competitive_observations
    ]
    risks = [
        _statement(
            risk.text,
            index=index,
            dossier=dossier,
            claim_ids=risk.evidence_claim_ids,
            unknown_refs=risk.unknown_references,
        )
        for risk in analysis.risks
    ]
    conflicts = [
        Statement(
            text=plain(conflict.summary), markers=index.markers_for(list(conflict.source_ids))
        )
        for conflict in dossier.conflicts
    ]
    open_questions = [
        Statement(text=plain(question), open_question=True) for question in analysis.open_questions
    ] + [
        Statement(text=plain(unknown.question), open_question=True) for unknown in dossier.unknowns
    ]

    return CompanyView(
        company_id=analysis.company_id,
        name=name,
        rank=rank,
        decision=DECISION_LABELS[recommendation.decision],
        decision_slug=recommendation.decision.value.replace("_", "-"),
        call_qualifier=call_label(kind, recommendation)[1],
        guardrail_ids=list(recommendation.guardrails_applied),
        score=analysis.total_score,
        maximum_score=MAX_TOTAL_SCORE,
        score_percent=round(100 * analysis.total_score / MAX_TOTAL_SCORE),
        score_width=bar_width(round(100 * analysis.total_score / MAX_TOTAL_SCORE)),
        maximum_achievable=headroom,
        meeting_reachable=headroom >= TAKE_A_MEETING_AT,
        confidence=confidence.level.value,
        confidence_score=round(confidence.score, 2),
        thesis_fit=FIT_LABELS[analysis.thesis_assessment.verdict],
        call_sentence=call_sentence(
            kind, name=name, analysis=analysis, recommendation=recommendation, dossier=dossier
        ),
        reading=reading(kind),
        disagreement=_disagreement(recommendation),
        website=safe_href(candidate.website if candidate else None),
        # From site/companies/<id>.html up to the run root, where memos/ lives.
        memo_href=internal_href(f"../../memos/{analysis.company_id}.md"),
        previous=previous,
        next=next_,
        snapshot=snapshot,
        rationale=[plain(line) for line in recommendation.rationale],
        guardrails=guardrails,
        thesis_line=thesis_line,
        sections=sections,
        competitive=competitive,
        score_rows=score_rows,
        risks=risks,
        conflicts=conflicts,
        open_questions=open_questions,
        identity_warnings=[plain(warning) for warning in analysis.identity_warnings],
        analysis_warnings=[plain(warning) for warning in analysis.analysis_warnings],
        changers=[plain(changer) for changer in analysis.recommendation_changers],
        sources=_source_rows(index),
        provenance=[
            Fact(
                "Evidence",
                f"{dossier.provider or 'unrecorded'} / {dossier.model or 'unrecorded'}",
                f"prompt {dossier.prompt_version or 'unrecorded'}",
            ),
            Fact(
                "Analysis",
                f"{analysis.provider} / {analysis.model}",
                f"prompt {analysis.prompt_version}",
            ),
            Fact("Thesis", analysis.thesis_version),
            Fact("Rubric", RUBRIC_VERSION),
            Fact("Policy", recommendation.policy_version),
            Fact("Site template", SITE_TEMPLATE_VERSION),
        ],
        unresolved_sources=sum(1 for row in _source_rows(index) if not row.resolved),
        page_warnings=list(index.warnings),
    )


def _short_gap(value: str) -> str:
    """``Not established`` in a table cell, the full phrase everywhere else."""
    return "Not established" if value in {"", NOT_ESTABLISHED} else value


def build_candidate_row(view: CompanyView) -> CandidateRow:
    """The portfolio row for a company, derived from its own page view.

    Derived rather than rebuilt so the two can never disagree: the table says exactly what
    the page it links to says.
    """
    searchable = " ".join(
        part
        for part in (
            view.name,
            view.company_id,
            next((f.value for f in view.snapshot if f.label.startswith("Product")), ""),
            next((f.value for f in view.snapshot if f.label == "Buyer"), ""),
            next((f.value for f in view.snapshot if f.label == "Workflow"), ""),
            view.thesis_fit,
        )
        if part
    ).lower()
    return CandidateRow(
        rank=view.rank,
        company_id=view.company_id,
        name=view.name,
        decision=view.decision,
        decision_slug=view.decision_slug,
        call_qualifier=view.call_qualifier,
        score=view.score,
        score_percent=view.score_percent,
        score_width=bar_width(view.score_percent),
        confidence=view.confidence,
        confidence_slug=view.confidence,
        thesis_fit=view.thesis_fit,
        thesis_slug=view.thesis_fit.split(",")[0].split(" ")[0].lower(),
        product=next((f.value for f in view.snapshot if f.label.startswith("Product")), ""),
        # The table repeats these two labels fifteen times, so the long phrase is
        # shortened here and only here. The company page keeps it in full.
        buyer=_short_gap(next((f.value for f in view.snapshot if f.label == "Buyer"), "")),
        workflow=_short_gap(next((f.value for f in view.snapshot if f.label == "Workflow"), "")),
        maximum_achievable=view.maximum_achievable,
        meeting_reachable=view.meeting_reachable,
        flags=[
            GUARDRAIL_CHIPS.get(guardrail, "Policy guardrail") for guardrail in view.guardrail_ids
        ]
        + (["Policy override"] if view.disagreement else []),
        disagreement=view.disagreement,
        detail_href=internal_href(f"companies/{view.company_id}.html"),
        website=view.website,
        search_text=searchable,
    )


def build_index_view(
    run_id: str,
    views: list[CompanyView],
    *,
    candidate_count: int,
    sources_cited: int,
    status_counts: dict[str, int],
    missing: list[str],
) -> IndexView:
    """Assemble the portfolio page from every company page that rendered."""
    rows = [build_candidate_row(view) for view in views]
    decisions = {
        DECISION_LABELS[decision]: sum(1 for view in views if view.decision_slug == slug)
        for decision, slug in (
            (Recommendation.TAKE_A_MEETING, "take-a-meeting"),
            (Recommendation.WATCH, "watch"),
            (Recommendation.PASS, "pass"),
        )
    }
    confidences = {
        level: sum(1 for view in views if view.confidence == level)
        for level in ("high", "medium", "low")
    }
    unreachable = [view for view in views if not view.meeting_reachable]
    summary = [
        Fact("Candidates", str(candidate_count)),
        *(Fact(label, str(total)) for label, total in decisions.items()),
        # Three labelled values rather than "1 / 10 / 4": a reader should not have to
        # decode a legend to learn how well-researched the run is.
        *(Fact(level.title(), str(confidences[level])) for level in ("high", "medium", "low")),
        Fact("Sources cited", str(sources_cited)),
    ]

    no_meeting: list[str] = []
    if not decisions[DECISION_LABELS[Recommendation.TAKE_A_MEETING]] and views:
        slots = sum(status_counts.values())
        breakdown = ", ".join(
            f"{status_counts.get(key, 0)} {label.lower()}"
            for key, label in (
                ("supported", "supported"),
                ("partially_supported", "partially supported"),
                ("contradicted", "contradicted"),
                ("not_assessable", "not assessable"),
            )
        )
        best = max(view.maximum_achievable for view in views)
        no_meeting = [
            f"No candidate reached the take-a-meeting band at {TAKE_A_MEETING_AT}/"
            f"{MAX_TOTAL_SCORE}.",
            f"Across {len(views)} analysed candidate(s), the {slots} scored dimension slots "
            f"were assessed as: {breakdown}.",
            f"For {len(unreachable)} of {len(views)} candidate(s) the recorded assessment "
            f"statuses capped the achievable total below {TAKE_A_MEETING_AT} before any "
            f"judgement about the company; the highest achievable total was {best}/"
            f"{MAX_TOTAL_SCORE}.",
            "No score was raised to produce a recommendation.",
        ]

    return IndexView(
        run_id=run_id,
        ranking_href=internal_href("../ranking.md"),
        summary=summary,
        thesis=THESIS_TEXT,
        thesis_version=THESIS_VERSION,
        thresholds=[
            f"Take a meeting at {TAKE_A_MEETING_AT}/{MAX_TOTAL_SCORE} and above, watch from "
            f"{WATCH_AT} to {TAKE_A_MEETING_AT - 1}, pass below {WATCH_AT}.",
            f"Research confidence is high at {CONFIDENCE_HIGH_AT:.2f} and above, medium from "
            f"{CONFIDENCE_MEDIUM_AT:.2f}, low below that.",
            "A dimension's assessment status caps what it may score: supported 100%, "
            "partially supported 70%, not assessable 50%, contradicted 100%.",
        ],
        evidence_note=(
            "A low score means the evidence available did not support a higher one. It is "
            "not a finding that the company is weak, and where a dimension could not be "
            "assessed at all the page says so rather than scoring it as a failure. The "
            "ordering below is a triage queue, not a quality ranking."
        ),
        rows=rows,
        filter_data=embed_json(
            [
                {
                    "id": row.company_id,
                    "rank": row.rank,
                    "name": row.name.lower(),
                    "call": row.decision_slug,
                    "confidence": row.confidence_slug,
                    "fit": row.thesis_slug,
                    "score": row.score,
                    "text": row.search_text,
                }
                for row in rows
            ]
        ),
        no_meeting=no_meeting,
        missing=missing,
        counts={
            "candidates": candidate_count,
            "analysed": len(views),
            "sources_cited": sources_cited,
            **decisions,
        },
    )


def _render(name: str, **context: object) -> str:
    """Render a template and normalise trailing whitespace, so bytes stay stable."""
    raw = html_environment().get_template(name).render(**context)
    lines = [line.rstrip() for line in raw.replace("\r\n", "\n").split("\n")]
    out: list[str] = []
    for line in lines:
        if not line and out and not out[-1]:
            continue
        out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out) + "\n"


def render_index(view: IndexView) -> str:
    return _render("site/index.html.j2", view=view)


def render_company(view: CompanyView) -> str:
    return _render("site/company.html.j2", view=view)
