"""Stage 6 - partner-ready memos and the portfolio ranking.

The only stage that makes no decision and no call. It reads validated artifacts - the
candidate set, the extracted pages, the evidence dossiers and the analyses with their
recommendations - and lays them out as Markdown a partner can read in a minute and a
reviewer can audit line by line.

Nothing here reaches the network, and nothing here consults a model. The score was
recomputed in Python, the confidence and the recommendation were produced by
:mod:`vc_scout.policy`, and the narrative was written under a validator that resolved
every citation. Rendering adds no fourth opinion on top of those three.

Failure is per candidate. A memo that cannot be rendered is recorded on the report, its
stale predecessor is removed so nothing out of date is left standing, and every other memo
and the ranking are still written.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass

from vc_scout.models.analysis import ceiling_for
from vc_scout.models.candidate import Candidate, CandidateSet
from vc_scout.models.page import PageBundle
from vc_scout.models.report import MemoFailure, MemoOutcome, RecommendationReport
from vc_scout.models.source import SourceReference
from vc_scout.policy import TAKE_A_MEETING_AT
from vc_scout.render.engine import TEMPLATE_VERSION
from vc_scout.render.memo import build_memo_view, memo_word_count, render_memo
from vc_scout.render.ranking import RankingInput, build_ranking_view, render_ranking, sort_key
from vc_scout.store import RunStore, StoreError

__all__ = ["MissingArtifactError", "RecommendStageOutcome", "run_recommend"]


class MissingArtifactError(RuntimeError):
    """Raised when an upstream artifact the whole stage depends on is absent.

    A run-level failure, unlike a single candidate that cannot be rendered: without the
    candidate set or an analysis report there is nothing to lay out at all.
    """


@dataclass(slots=True)
class RecommendStageOutcome:
    """What the stage produced, for the CLI to summarise."""

    report: RecommendationReport
    report_path: str
    ranking_path: str
    memo_paths: list[str]


def _pages(store: RunStore, company_id: str) -> PageBundle | None:
    """Extracted pages, when the enrichment stage wrote any for this company."""
    try:
        return store.read_pages(company_id)
    except StoreError:
        return None


def run_recommend(*, store: RunStore) -> RecommendStageOutcome:
    """Render every memo this run can support, then the ranking, then the report."""
    if not store.candidates_path().exists():
        raise MissingArtifactError(
            f"run {store.run_id!r} has no candidates.json; run `vc-scout source` first"
        )
    if not store.analysis_report_path().exists():
        raise MissingArtifactError(
            f"run {store.run_id!r} has no analysis-report.json; run `vc-scout analyze` first"
        )

    candidate_set: CandidateSet = store.read_candidates()
    candidate_sources = candidate_set.source_index()

    items: list[RankingInput] = []
    memos: list[MemoOutcome] = []
    failures: list[MemoFailure] = []
    warnings: list[str] = []
    memo_paths: list[str] = []

    for candidate in candidate_set.candidates:
        outcome = _render_one(
            store,
            candidate=candidate,
            candidate_sources=candidate_sources,
        )
        if isinstance(outcome, MemoFailure):
            failures.append(outcome)
            if store.delete_memo(candidate.company_id):
                # A candidate that failed in this run must not keep a memo from an earlier
                # one: the ranking will not link to it, and nothing else marks it stale.
                warnings.append(
                    f"{candidate.company_id}: removed a memo left over from an earlier run, "
                    "because this run could not render one"
                )
            continue
        memo, item = outcome
        memos.append(memo)
        items.append(item)
        memo_paths.append(memo.memo_path)

    missing = [
        f"**{failure.company_id}** - {failure.reason}"
        + (f" ({failure.detail})" if failure.detail else "")
        for failure in failures
    ]
    view = build_ranking_view(
        store.run_id,
        items,
        candidate_count=len(candidate_set.candidates),
        missing=missing,
    )
    ranking_path = store.write_text(store.ranking_path(), render_ranking(view))

    ordered = [item.analysis.company_id for item in sorted(items, key=sort_key)]
    by_id = {memo.company_id: memo for memo in memos}
    report = RecommendationReport(
        run_id=store.run_id,
        template_version=TEMPLATE_VERSION,
        candidate_count=len(candidate_set.candidates),
        memos_written=len(memos),
        ranking_path=store.relative(ranking_path),
        ordered_company_ids=ordered,
        recommendations=dict(
            sorted(Counter(item.recommendation.decision.value for item in items).items())
        ),
        score_range=(
            {
                "min": min(memo.total_score for memo in memos),
                "max": max(memo.total_score for memo in memos),
            }
            if memos
            else {}
        ),
        confidence_counts=dict(
            sorted(Counter(item.recommendation.confidence.level.value for item in items).items())
        ),
        guardrail_counts=dict(
            sorted(
                Counter(
                    guardrail
                    for item in items
                    for guardrail in item.recommendation.guardrails_applied
                ).items()
            )
        ),
        component_status_counts=dict(
            sorted(
                Counter(
                    component.assessment_status.value
                    for item in items
                    for component in item.analysis.score_components
                ).items()
            )
        ),
        model_policy_disagreements=sum(1 for item in items if item.recommendation.model_disagreed),
        referenced_sources=sum(memo.sources_referenced for memo in memos),
        missing_source_metadata=sum(memo.unresolved_sources for memo in memos),
        candidates_with_meeting_unreachable=sum(
            1 for memo in memos if memo.maximum_achievable_score < TAKE_A_MEETING_AT
        ),
        memos=[by_id[company_id] for company_id in ordered],
        warnings=warnings + [w for memo in memos for w in memo.warnings],
        failures=failures,
    )
    report_path = store.write_recommendation_report(report)
    return RecommendStageOutcome(
        report=report,
        report_path=store.relative(report_path),
        ranking_path=store.relative(ranking_path),
        memo_paths=memo_paths,
    )


def _render_one(
    store: RunStore,
    *,
    candidate: Candidate,
    candidate_sources: dict[str, SourceReference],
) -> tuple[MemoOutcome, RankingInput] | MemoFailure:
    """Render one candidate's memo, or record why it could not be rendered."""
    company_id = candidate.company_id
    try:
        analysis, recommendation = store.read_analysis(company_id)
    except StoreError:
        return MemoFailure(
            company_id=company_id,
            reason="no analysis was produced for this candidate",
            detail="see analysis-report.json for the recorded failure",
        )
    if recommendation is None:
        return MemoFailure(
            company_id=company_id,
            reason="the analysis carries no recommendation",
            detail="the policy stage did not complete for this candidate",
        )
    try:
        dossier = store.read_evidence(company_id)
    except StoreError:
        return MemoFailure(
            company_id=company_id,
            reason="no evidence dossier exists for this candidate",
            detail="a memo may not be rendered from an analysis whose evidence is missing",
        )

    try:
        view = build_memo_view(
            candidate=candidate,
            candidate_sources=candidate_sources,
            pages=_pages(store, company_id),
            dossier=dossier,
            analysis=analysis,
            recommendation=recommendation,
        )
        markdown = render_memo(view)
        path = store.write_memo(company_id, markdown)
    except Exception as exc:  # noqa: BLE001 - one memo must never fail the whole stage.
        return MemoFailure(
            company_id=company_id,
            reason="rendering failed",
            detail=f"unexpected {type(exc).__name__}",
        )

    headroom = sum(
        ceiling_for(component.component, component.assessment_status)
        for component in analysis.score_components
    )
    memo = MemoOutcome(
        company_id=company_id,
        memo_path=store.relative(path),
        words=memo_word_count(markdown),
        sources_referenced=len(view.sources),
        unresolved_sources=sum(1 for source in view.sources if not source.resolved),
        decision=recommendation.decision,
        total_score=analysis.total_score,
        maximum_achievable_score=headroom,
        warnings=list(view.warnings),
    )
    item = RankingInput(
        candidate=candidate,
        analysis=analysis,
        recommendation=recommendation,
        dossier=dossier,
        memo_relative_path=store.relative(path),
    )
    return memo, item
