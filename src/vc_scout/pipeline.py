"""The one-command pipeline: source, enrich, extract, analyse, render, publish.

This module is deliberately thin. It owns three things and nothing else:

* **order** - which stage runs after which, and what flows between them;
* **resume** - whether a stage's artifacts are current enough to skip, decided by
  fingerprint rather than by the presence of a file;
* **stop and continue** - which failures end the run and which are per-candidate facts the
  run carries forward.

Every stage is called through its existing entry point. There is no second implementation
of sourcing, enrichment, extraction, analysis, rendering or the site here, and adding one
would be the bug: two copies of a stage drift, and the drift is invisible until an artifact
disagrees with the report describing it.

The resume rule is conservative in one specific direction. An artifact whose provenance
cannot be established - written by a single-stage command, or carrying no fingerprint -
is rerun, never trusted. Re-running costs time; rendering last week's analysis over this
week's evidence costs a partner's confidence in every number on the page.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from vc_scout.assessment_policy import ASSESSMENT_POLICY_VERSION
from vc_scout.discovery import DISCOVERY_FORMULA_VERSION
from vc_scout.fingerprint import (
    analysis_fingerprint,
    candidates_fingerprint,
    enrichment_fingerprint,
    evidence_fingerprint,
    recommendation_fingerprint,
)
from vc_scout.llm.provider import LlmProvider, ModelConfig
from vc_scout.models.enums import PipelineStage, PipelineStageStatus
from vc_scout.models.report import RunReport, StageRun, UiReport
from vc_scout.net.hn import HnAlgoliaClient, HnError
from vc_scout.net.http import SafeFetcher
from vc_scout.policy import POLICY_VERSION
from vc_scout.prompts import EVIDENCE_PROMPT_VERSION
from vc_scout.render.engine import TEMPLATE_VERSION as MEMO_TEMPLATE_VERSION
from vc_scout.render.html import SITE_TEMPLATE_VERSION
from vc_scout.rubric import RUBRIC_VERSION
from vc_scout.stages.analysis import ANALYSIS_PROMPT_VERSION, run_analysis
from vc_scout.stages.enrich import MAX_EXTRA_PAGES, run_enrich
from vc_scout.stages.evidence import run_evidence
from vc_scout.stages.recommend import run_recommend
from vc_scout.stages.source import DEFAULT_WINDOW_DAYS, run_source
from vc_scout.stages.ui import run_build_ui
from vc_scout.store import RunStore, StoreError
from vc_scout.thesis import THESIS_VERSION

__all__ = [
    "STAGE_ORDER",
    "PipelineAbortedError",
    "PipelineResult",
    "Plan",
    "downstream_of",
    "run_pipeline",
]

STAGE_ORDER: tuple[PipelineStage, ...] = tuple(PipelineStage)


class PipelineAbortedError(RuntimeError):
    """A run-level failure: the run cannot usefully continue.

    Raised only for the two conditions that make everything downstream meaningless -
    discovery failing outright, and discovery finding nobody.
    """


def downstream_of(stage: PipelineStage) -> tuple[PipelineStage, ...]:
    """Every stage that consumes ``stage``'s output, directly or transitively."""
    return STAGE_ORDER[STAGE_ORDER.index(stage) + 1 :]


@dataclass(slots=True)
class Plan:
    """What the caller asked for. Validated before a run directory is created."""

    query: str
    limit: int
    provider_name: str
    model: str
    effort: str
    max_extra_pages: int = MAX_EXTRA_PAGES
    window_days: int = DEFAULT_WINDOW_DAYS
    max_tokens: int = 8192
    forced: frozenset[PipelineStage] = frozenset()
    stop_after: PipelineStage | None = None

    def stages(self) -> tuple[PipelineStage, ...]:
        if self.stop_after is None:
            return STAGE_ORDER
        return STAGE_ORDER[: STAGE_ORDER.index(self.stop_after) + 1]


@dataclass(slots=True)
class PipelineResult:
    """What the run produced, for the CLI to summarise."""

    report: RunReport
    report_path: str
    #: Stages that actually executed, as opposed to being resumed or skipped.
    executed: list[PipelineStage] = field(default_factory=list)


@dataclass(slots=True)
class _Context:
    """Mutable state threaded through the run."""

    store: RunStore
    plan: Plan
    provider: LlmProvider
    config: ModelConfig
    client_factory: Callable[[], HnAlgoliaClient]
    fetcher_factory: Callable[[], SafeFetcher]
    now: datetime
    #: Stages whose artifacts must be rebuilt, because they were forced or because
    #: something upstream of them ran.
    invalid: set[PipelineStage] = field(default_factory=set)
    runs: list[StageRun] = field(default_factory=list)
    tokens: dict[str, int] = field(default_factory=dict)
    failures: dict[str, int] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    #: True once a run-level provider failure has been seen. Everything that needs the
    #: provider stops; everything that does not carries on over what exists.
    provider_failed: bool = False


# -- resume checks -----------------------------------------------------------
#
# Each returns the reason a stage may be skipped, or None to run it. They read artifacts
# and never write, so a resume decision can never itself change the run.


def _expected_ids(store: RunStore) -> list[str]:
    return [candidate.company_id for candidate in store.read_candidates().candidates]


def _resume_source(store: RunStore) -> str | None:
    if not store.candidates_path().exists() or not store.source_report_path().exists():
        return None
    try:
        candidates = store.read_candidates()
        store.read_source_report()
    except (StoreError, ValueError):
        return None
    if not candidates.candidates:
        return None
    return f"{len(candidates.candidates)} candidate(s) already discovered"


def _resume_enrich(store: RunStore) -> str | None:
    try:
        report = store.read_enrichment_report()
        expected = _expected_ids(store)
        bundles = [store.read_pages(company_id) for company_id in expected]
    except (StoreError, ValueError):
        return None
    if report.upstream_fingerprint != candidates_fingerprint(store.read_candidates()):
        return None
    if {row.company_id for row in report.candidates} != set(expected):
        return None
    return f"{len(bundles)} page bundle(s) current for these candidates"


def _resume_evidence(store: RunStore) -> str | None:
    try:
        report = store.read_evidence_report()
        expected = _expected_ids(store)
        bundles = [store.read_pages(company_id) for company_id in expected]
    except (StoreError, ValueError):
        return None
    if report.upstream_fingerprint != enrichment_fingerprint(bundles):
        return None
    if {row.company_id for row in report.candidates} != set(expected):
        return None
    return f"{report.counts.get('succeeded', 0)} dossier(s) current for this enrichment"


def _resume_analysis(store: RunStore) -> str | None:
    try:
        report = store.read_analysis_report()
        expected = _expected_ids(store)
        dossiers = [
            store.read_evidence(company_id)
            for company_id in expected
            if store.evidence_path(company_id).is_file()
        ]
    except (StoreError, ValueError):
        return None
    if report.filtered_to is not None:
        return None
    if report.upstream_fingerprint != evidence_fingerprint(dossiers):
        return None
    if {row.company_id for row in report.candidates} != set(expected):
        return None
    return f"{report.counts.get('succeeded', 0)} analysis file(s) current for this evidence"


def _resume_recommend(store: RunStore) -> str | None:
    try:
        report = store.read_recommendation_report()
        analyses = [store.read_analysis(company_id) for company_id in store.analysis_company_ids()]
    except (StoreError, ValueError):
        return None
    pairs = [(a, r) for a, r in analyses if r is not None]
    if report.upstream_fingerprint != analysis_fingerprint(pairs):
        return None
    if not store.ranking_path().is_file():
        return None
    return f"{report.memos_written} memo(s) current for these analyses"


def _resume_ui(store: RunStore) -> str | None:
    try:
        report = store.read_model(store.site_dir / "ui-report.json", UiReport)
        recommendation = store.read_recommendation_report()
    except (StoreError, ValueError):
        return None
    expected = recommendation_fingerprint(
        recommendation.ordered_company_ids, recommendation.template_version
    )
    if report.upstream_fingerprint != expected:
        return None
    if not (store.site_dir / "index.html").is_file():
        return None
    return f"{report.pages_written} page(s) current for these memos"


_RESUME: dict[PipelineStage, Callable[[RunStore], str | None]] = {
    PipelineStage.SOURCE: _resume_source,
    PipelineStage.ENRICH: _resume_enrich,
    PipelineStage.EVIDENCE: _resume_evidence,
    PipelineStage.ANALYSIS: _resume_analysis,
    PipelineStage.RECOMMEND: _resume_recommend,
    PipelineStage.UI: _resume_ui,
}


# -- stage execution ---------------------------------------------------------


def _stamp(store: RunStore, stage: PipelineStage) -> str:
    """Record on a derived report the fingerprint of what it was derived from.

    Stamped here rather than inside each stage so the stage modules stay unaware of the
    orchestration, and so an artifact written by a single-stage command carries no
    fingerprint - which a later resume reads, correctly, as unknown provenance.
    """
    if stage is PipelineStage.ENRICH:
        value = candidates_fingerprint(store.read_candidates())
        enrichment = store.read_enrichment_report()
        store.write_enrichment_report(enrichment.model_copy(update={"upstream_fingerprint": value}))
    elif stage is PipelineStage.EVIDENCE:
        value = enrichment_fingerprint(
            [store.read_pages(cid) for cid in store.extracted_company_ids()]
        )
        evidence = store.read_evidence_report()
        store.write_evidence_report(evidence.model_copy(update={"upstream_fingerprint": value}))
    elif stage is PipelineStage.ANALYSIS:
        value = evidence_fingerprint(
            [store.read_evidence(cid) for cid in store.evidence_company_ids()]
        )
        analysis_report = store.read_analysis_report()
        store.write_analysis_report(
            analysis_report.model_copy(update={"upstream_fingerprint": value})
        )
    elif stage is PipelineStage.RECOMMEND:
        pairs = [
            (a, r)
            for a, r in (store.read_analysis(cid) for cid in store.analysis_company_ids())
            if r is not None
        ]
        value = analysis_fingerprint(pairs)
        recommendation = store.read_recommendation_report()
        store.write_recommendation_report(
            recommendation.model_copy(update={"upstream_fingerprint": value})
        )
    elif stage is PipelineStage.UI:
        recommendation = store.read_recommendation_report()
        value = recommendation_fingerprint(
            recommendation.ordered_company_ids, recommendation.template_version
        )
        ui = store.read_model(store.site_dir / "ui-report.json", UiReport)
        store.write_model(
            store.site_dir / "ui-report.json",
            ui.model_copy(update={"upstream_fingerprint": value}),
        )
    else:
        value = candidates_fingerprint(store.read_candidates())
    return value


def _run_source(context: _Context) -> StageRun:
    plan, store = context.plan, context.store
    try:
        with context.client_factory() as client:
            outcome = run_source(
                store=store,
                client=client,
                query=plan.query,
                limit=plan.limit,
                window_days=plan.window_days,
            )
    except HnError as exc:
        raise PipelineAbortedError(f"discovery failed: {exc}") from exc

    kept = len(outcome.candidates.candidates)
    if kept == 0:
        raise PipelineAbortedError(
            "discovery produced no candidates; see source-report.json for the funnel"
        )
    context.warnings.extend(outcome.report.failures)
    return StageRun(
        stage=PipelineStage.SOURCE,
        status=(
            PipelineStageStatus.PARTIAL
            if outcome.report.shortfall
            else PipelineStageStatus.COMPLETED
        ),
        candidates_in=plan.limit,
        candidates_out=kept,
        failures=len(outcome.report.failures),
        artifacts=[outcome.candidates_path, outcome.report_path],
        decision=(
            f"kept {kept} of a requested {plan.limit}"
            + (
                f"; {outcome.report.shortfall} place(s) unfilled"
                if outcome.report.shortfall
                else ""
            )
        ),
    )


def _run_enrich(context: _Context) -> StageRun:
    store = context.store
    with context.fetcher_factory() as fetcher:
        outcome = run_enrich(
            store=store, fetcher=fetcher, max_extra_pages=context.plan.max_extra_pages
        )
    counts = outcome.report.counts
    blind = sum(1 for row in outcome.report.candidates if row.pages_extracted == 0)
    for category, total in outcome.report.failures_by_category.items():
        key = f"enrich.{category}"
        context.failures[key] = context.failures.get(key, 0) + total
    return StageRun(
        stage=PipelineStage.ENRICH,
        # Enrichment never drops a candidate: a company with an unreachable site stays in
        # the run with zero pages, which is a fact the later stages must see.
        status=PipelineStageStatus.PARTIAL if blind else PipelineStageStatus.COMPLETED,
        candidates_in=counts.get("candidates", 0),
        candidates_out=counts.get("candidates", 0),
        failures=blind,
        artifacts=[outcome.report_path],
        decision=(
            f"{counts.get('pages_extracted', 0)} page(s) read; "
            f"{blind} candidate(s) with no readable page, retained"
        ),
    )


def _llm_stage(context: _Context, stage: PipelineStage, *, prefix: str) -> tuple[StageRun, bool]:
    """Run evidence or analysis, and report whether the provider failed at run level."""
    store = context.store
    # Both stages report the same shape - counts, failures by category, one report path -
    # so the two are read through that shape rather than branching all the way down.
    if stage is PipelineStage.EVIDENCE:
        evidence_outcome = run_evidence(
            store=store, provider=context.provider, config=context.config
        )
        counts = evidence_outcome.report.counts
        by_category = evidence_outcome.report.failures_by_category
        produced = len(evidence_outcome.dossiers)
        artifacts = [evidence_outcome.report_path]
    else:
        analysis_outcome = run_analysis(
            store=store, provider=context.provider, config=context.config
        )
        counts = analysis_outcome.report.counts
        by_category = analysis_outcome.report.failures_by_category
        produced = len(analysis_outcome.analyses)
        artifacts = [analysis_outcome.report_path]

    context.tokens[f"{prefix}_input_tokens"] = counts.get("input_tokens", 0)
    context.tokens[f"{prefix}_output_tokens"] = counts.get("output_tokens", 0)
    for category, total in by_category.items():
        key = f"{prefix}.{category}"
        context.failures[key] = context.failures.get(key, 0) + total

    total = counts.get("candidates", 0)
    aborted = bool(counts.get("run_aborted"))
    if aborted:
        status = PipelineStageStatus.FAILED
        decision = (
            "the provider reported a run-level failure; every remaining candidate would "
            "have failed identically, so the stage stopped"
        )
    elif produced == 0:
        status = PipelineStageStatus.FAILED
        decision = f"no output for any of {total} candidate(s)"
    elif produced < total:
        status = PipelineStageStatus.PARTIAL
        decision = f"{produced} of {total} candidate(s) succeeded; the rest are recorded failures"
    else:
        status = PipelineStageStatus.COMPLETED
        decision = f"all {total} candidate(s) succeeded"

    return (
        StageRun(
            stage=stage,
            status=status,
            candidates_in=total,
            candidates_out=produced,
            failures=total - produced,
            artifacts=artifacts,
            decision=decision,
        ),
        aborted,
    )


def _run_recommend(context: _Context) -> StageRun:
    outcome = run_recommend(store=context.store)
    report = outcome.report
    context.warnings.extend(report.warnings)
    for failure in report.failures:
        key = f"recommend.{failure.reason.split(';')[0][:40]}"
        context.failures[key] = context.failures.get(key, 0) + 1
    status = (
        PipelineStageStatus.FAILED
        if report.memos_written == 0
        else PipelineStageStatus.PARTIAL
        if report.failures
        else PipelineStageStatus.COMPLETED
    )
    return StageRun(
        stage=PipelineStage.RECOMMEND,
        status=status,
        candidates_in=report.candidate_count,
        candidates_out=report.memos_written,
        failures=len(report.failures),
        artifacts=[outcome.ranking_path, outcome.report_path],
        decision=f"{report.memos_written} of {report.candidate_count} memo(s) rendered",
    )


def _run_ui(context: _Context) -> StageRun:
    outcome = run_build_ui(store=context.store, force=True)
    report = outcome.report
    context.warnings.extend(report.warnings)
    status = PipelineStageStatus.PARTIAL if report.failures else PipelineStageStatus.COMPLETED
    return StageRun(
        stage=PipelineStage.UI,
        status=status,
        candidates_in=report.candidate_count,
        candidates_out=len(report.company_pages),
        failures=len(report.failures),
        artifacts=[outcome.index_path, outcome.report_path],
        decision=f"{report.pages_written} page(s) generated",
    )


# -- the run -----------------------------------------------------------------


def run_pipeline(
    *,
    store: RunStore,
    plan: Plan,
    provider: LlmProvider,
    client_factory: Callable[[], HnAlgoliaClient] = HnAlgoliaClient,
    fetcher_factory: Callable[[], SafeFetcher] = SafeFetcher,
    now: datetime | None = None,
    on_stage: Callable[[StageRun], None] | None = None,
) -> PipelineResult:
    """Execute the pipeline end to end, resuming what is already current.

    ``client_factory`` and ``fetcher_factory`` are injected so the whole pipeline can be
    exercised against fixtures without a socket. The fake LLM provider does not make the
    network stages offline - those are separate dependencies and are injected separately.
    """
    started = (now or datetime.now(UTC)).astimezone(UTC)
    config = ModelConfig(model=plan.model, max_tokens=plan.max_tokens, effort=plan.effort)
    context = _Context(
        store=store,
        plan=plan,
        provider=provider,
        config=config,
        client_factory=client_factory,
        fetcher_factory=fetcher_factory,
        now=started,
        invalid=set(_expand_forced(plan.forced)),
    )
    store.ensure_root()

    executed: list[PipelineStage] = []
    resumability: dict[str, str] = {}
    aborted_reason: str | None = None

    for stage in plan.stages():
        record = _execute(context, stage, executed)
        context.runs.append(record)
        resumability[stage.value] = _resumability_line(record)
        if on_stage is not None:
            on_stage(record)
        if record.status is PipelineStageStatus.FAILED and stage is PipelineStage.SOURCE:
            aborted_reason = record.decision
            break

    completed = datetime.now(UTC) if now is None else now
    report = _build_report(
        context,
        started=started,
        completed=completed,
        resumability=resumability,
        aborted_reason=aborted_reason,
    )
    path = store.write_run_report(report)
    return PipelineResult(report=report, report_path=store.relative(path), executed=executed)


def _expand_forced(forced: Iterable[PipelineStage]) -> set[PipelineStage]:
    """Forcing a stage invalidates it and everything below it, never anything above it."""
    expanded: set[PipelineStage] = set()
    for stage in forced:
        expanded.add(stage)
        expanded.update(downstream_of(stage))
    return expanded


def _execute(context: _Context, stage: PipelineStage, executed: list[PipelineStage]) -> StageRun:
    """Resume, skip or run one stage, and record which and why."""
    store = context.store

    blocked = _blocked(context, stage)
    if blocked is not None:
        return StageRun(stage=stage, status=PipelineStageStatus.SKIPPED, decision=blocked)

    if stage not in context.invalid and (reason := _RESUME[stage](store)) is not None:
        return StageRun(
            stage=stage,
            status=PipelineStageStatus.COMPLETED,
            resumed=True,
            decision=f"resumed: {reason}",
            upstream_fingerprint=_current_fingerprint(store, stage),
        )

    # This stage is about to run, so nothing derived from it can still be current.
    context.invalid.update(downstream_of(stage))
    started = time.monotonic()
    try:
        record = _dispatch(context, stage)
    except PipelineAbortedError:
        raise
    except (StoreError, ValueError) as exc:
        record = StageRun(
            stage=stage,
            status=PipelineStageStatus.FAILED,
            decision=f"{type(exc).__name__}: {exc}",
        )
    executed.append(stage)
    record = record.model_copy(update={"duration_seconds": round(time.monotonic() - started, 3)})
    if record.status is not PipelineStageStatus.FAILED and stage is not PipelineStage.SOURCE:
        record = record.model_copy(update={"upstream_fingerprint": _stamp(store, stage)})
    return record


def _dispatch(context: _Context, stage: PipelineStage) -> StageRun:
    if stage is PipelineStage.SOURCE:
        return _run_source(context)
    if stage is PipelineStage.ENRICH:
        return _run_enrich(context)
    if stage in (PipelineStage.EVIDENCE, PipelineStage.ANALYSIS):
        prefix = "evidence" if stage is PipelineStage.EVIDENCE else "analysis"
        record, aborted = _llm_stage(context, stage, prefix=prefix)
        if aborted:
            context.provider_failed = True
        return record
    if stage is PipelineStage.RECOMMEND:
        return _run_recommend(context)
    return _run_ui(context)


def _blocked(context: _Context, stage: PipelineStage) -> str | None:
    """Why this stage cannot run at all, if it cannot."""
    store = context.store
    if context.provider_failed and stage in (PipelineStage.EVIDENCE, PipelineStage.ANALYSIS):
        return "skipped: a run-level provider failure stops every stage that needs the model"
    if stage in (PipelineStage.RECOMMEND, PipelineStage.UI) and not (
        store.analysis_report_path().exists()
    ):
        return "skipped: no analysis report exists, so there is nothing to render"
    if stage is PipelineStage.UI and not store.recommendation_report_path().exists():
        return "skipped: no recommendation report exists, so there is nothing to publish"
    return None


def _current_fingerprint(store: RunStore, stage: PipelineStage) -> str | None:
    """The fingerprint a resumed stage's report already carries."""
    try:
        if stage is PipelineStage.ENRICH:
            return store.read_enrichment_report().upstream_fingerprint
        if stage is PipelineStage.EVIDENCE:
            return store.read_evidence_report().upstream_fingerprint
        if stage is PipelineStage.ANALYSIS:
            return store.read_analysis_report().upstream_fingerprint
        if stage is PipelineStage.RECOMMEND:
            return store.read_recommendation_report().upstream_fingerprint
        if stage is PipelineStage.UI:
            return store.read_model(
                store.site_dir / "ui-report.json", UiReport
            ).upstream_fingerprint
    except (StoreError, ValueError):
        return None
    return None


def _resumability_line(record: StageRun) -> str:
    if record.status is PipelineStageStatus.SKIPPED:
        return record.decision or "skipped"
    if record.resumed:
        return record.decision or "resumed"
    return f"ran: {record.decision or record.status.value}"


def _build_report(
    context: _Context,
    *,
    started: datetime,
    completed: datetime,
    resumability: dict[str, str],
    aborted_reason: str | None,
) -> RunReport:
    store, plan = context.store, context.plan
    recommendations: dict[str, int] = {}
    site_path = ranking_path = memos_path = None
    artifacts: dict[str, str] = {}

    for name, path in (
        ("candidates", store.candidates_path()),
        ("source_report", store.source_report_path()),
        ("enrichment_report", store.enrichment_report_path()),
        ("evidence_report", store.evidence_report_path()),
        ("analysis_report", store.analysis_report_path()),
        ("recommendation_report", store.recommendation_report_path()),
        ("ui_report", store.site_dir / "ui-report.json"),
    ):
        if path.exists():
            artifacts[name] = store.relative(path)

    if store.recommendation_report_path().exists():
        report = store.read_recommendation_report()
        recommendations = dict(report.recommendations)
        ranking_path = report.ranking_path
        memos_path = "memos"
    if (store.site_dir / "index.html").is_file():
        site_path = store.relative(store.site_dir)

    flow: dict[str, int] = {}
    for record in context.runs:
        if record.candidates_out or record.candidates_in:
            flow[f"{record.stage.value}_in"] = record.candidates_in
            flow[f"{record.stage.value}_out"] = record.candidates_out

    warnings = list(context.warnings)
    if aborted_reason:
        warnings.insert(0, f"run stopped: {aborted_reason}")

    return RunReport(
        run_id=store.run_id,
        query=plan.query,
        requested_limit=plan.limit,
        provider=plan.provider_name,
        model=plan.model,
        effort=plan.effort,
        started_at=started,
        completed_at=completed,
        duration_seconds=round(sum(record.duration_seconds for record in context.runs), 3),
        stages=context.runs,
        candidate_flow=flow,
        recommendations=recommendations,
        token_usage=dict(sorted(context.tokens.items())),
        failure_summary=dict(sorted(context.failures.items())),
        warnings=warnings,
        site_path=site_path,
        ranking_path=ranking_path,
        memos_path=memos_path,
        artifacts=artifacts,
        versions={
            "source_formula": DISCOVERY_FORMULA_VERSION,
            "evidence_prompt": EVIDENCE_PROMPT_VERSION,
            "analysis_prompt": ANALYSIS_PROMPT_VERSION,
            "assessment_policy": ASSESSMENT_POLICY_VERSION,
            "thesis": THESIS_VERSION,
            "rubric": RUBRIC_VERSION,
            "policy": POLICY_VERSION,
            "memo_template": MEMO_TEMPLATE_VERSION,
            "ui_template": SITE_TEMPLATE_VERSION,
        },
        resumability=resumability,
        stopped_after=plan.stop_after,
        forced_stages=sorted(plan.forced, key=STAGE_ORDER.index),
    )
