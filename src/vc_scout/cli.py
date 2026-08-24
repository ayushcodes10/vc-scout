"""Command-line interface.

Every command listed here is part of the delivered contract. In this stage the pipeline
commands are placeholders: they parse their arguments, report which implementation stage
owns them, and exit non-zero. They do not pretend to have done work.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

import typer

from vc_scout import __version__
from vc_scout.config import (
    API_KEY_ENV,
    DEFAULT_EFFORT,
    DEFAULT_LIMIT,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    DEFAULT_TIMEOUT_SECONDS,
    MODEL_ENV,
)
from vc_scout.demo_fixtures import DEMO_QUERY, demo_client, demo_fetcher
from vc_scout.llm.anthropic import AnthropicProvider
from vc_scout.llm.fake import FakeProvider
from vc_scout.llm.provider import LlmProvider, ModelConfig
from vc_scout.models.enums import PipelineStage, PipelineStageStatus
from vc_scout.models.report import StageRun
from vc_scout.net.hn import HnAlgoliaClient, HnError
from vc_scout.net.http import SafeFetcher
from vc_scout.pipeline import (
    STAGE_ORDER,
    PipelineAbortedError,
    PipelineResult,
    Plan,
    apply_recovery,
    run_pipeline,
)
from vc_scout.policy import POLICY_VERSION, TAKE_A_MEETING_AT, WATCH_AT
from vc_scout.rubric import RUBRIC, RUBRIC_VERSION
from vc_scout.stages.analysis import (
    MAX_ATTEMPTS,
    AnalysisStageOutcome,
    UnknownCandidateError,
    run_analysis,
)
from vc_scout.stages.enrich import MAX_EXTRA_PAGES, EnrichOutcome, run_enrich
from vc_scout.stages.evidence import EvidenceStageOutcome, run_evidence
from vc_scout.stages.export import ExportError, export_demo
from vc_scout.stages.recommend import (
    MissingArtifactError,
    RecommendStageOutcome,
    run_recommend,
)
from vc_scout.stages.recover import (
    RecoveryError,
    RecoveryOutcome,
    plan_recovery,
    recover_analyses,
)
from vc_scout.stages.source import DEFAULT_WINDOW_DAYS, SourceOutcome, run_source
from vc_scout.stages.ui import MissingArtifactError as UiArtifactError
from vc_scout.stages.ui import UiStageOutcome, run_build_ui
from vc_scout.store import RunStore, StoreError

__all__ = ["app", "main"]

app = typer.Typer(
    name="vc-scout",
    help="AI-augmented investment triage for a seed-stage VC firm.",
    no_args_is_help=True,
    add_completion=False,
)

_RUN_ID = typer.Option(
    ..., "--run-id", help="Identifier for this run, used as the output directory."
)
#: Module-level singletons: Typer reads a call's result as the default, and ruff refuses a
#: call inside an argument default. Defining them once here satisfies both.
_FORCE_STAGE = typer.Option(
    [],
    "--force-stage",
    help=(
        "Rerun this stage and everything downstream of it. Repeatable. One of: "
        + ", ".join(stage.value for stage in PipelineStage)
    ),
)
_RECOVER_COMPANY_ID = typer.Option(
    [],
    "--company-id",
    help="Recover only this candidate. Repeatable. Must be recorded as failed.",
)
_DESTINATION = typer.Option(
    Path("demo"), "--destination", help="Directory to write. Intended to be committed."
)

_RUNS_ROOT = typer.Option(
    Path("outputs/runs"), "--runs-root", help="Root directory holding all runs."
)

#: Exit code used by a command that is declared but not yet implemented.
NOT_IMPLEMENTED_EXIT = 2

#: The `run` command shortlists inside this band. Fewer than ten is not a pipeline worth
#: reviewing; more than twenty is a cost decision that should be made deliberately.
MIN_RUN_LIMIT = 10
MAX_RUN_LIMIT = 20

#: The offline demo's shortlist size, bounded by the committed fixture corpus.
DEMO_LIMIT = 10


def _placeholder(command: str, stage: str, does: str) -> None:
    """Report honestly that a declared command has no implementation yet."""
    typer.secho(f"vc-scout {command}: not implemented yet.", fg=typer.colors.YELLOW, err=True)
    typer.secho(f"  planned behaviour: {does}", err=True)
    typer.secho(f"  implemented in: {stage} (see docs/PLAN.md)", err=True)
    raise typer.Exit(code=NOT_IMPLEMENTED_EXIT)


@app.command()
def source(
    query: str = typer.Option(..., "--query", help="Thesis-relevant search query."),
    limit: int = typer.Option(DEFAULT_LIMIT, "--limit", min=1, help="Maximum candidates to keep."),
    run_id: str = _RUN_ID,
    runs_root: Path = _RUNS_ROOT,
    window_days: int = typer.Option(
        DEFAULT_WINDOW_DAYS, "--window-days", min=1, help="How far back to search."
    ),
    force: bool = typer.Option(False, "--force", help="Recompute even if artifacts exist."),
) -> None:
    """Discover candidate startups and write candidates.json."""
    try:
        store = RunStore(run_id, runs_root=runs_root)
    except StoreError as exc:
        typer.secho(f"vc-scout source: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    if store.candidates_path().exists() and not force:
        typer.secho(
            f"vc-scout source: {store.relative(store.candidates_path())} already exists in run "
            f"{run_id!r}. Pass --force to re-run discovery.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        with HnAlgoliaClient() as client:
            outcome = run_source(
                store=store, client=client, query=query, limit=limit, window_days=window_days
            )
    except HnError as exc:
        typer.secho(f"vc-scout source: discovery failed: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    _report_source(outcome, limit=limit)


def _report_source(outcome: SourceOutcome, *, limit: int) -> None:
    """Print the discovery funnel and shortlist. Exits non-zero when nothing was found."""
    report = outcome.report
    counts = report.counts
    kept = len(outcome.candidates.candidates)

    typer.echo(
        f"Fetched {counts.get('hits_fetched', 0)} hits across {len(report.variants)} "
        f"query variants."
    )
    for key in sorted(k for k in counts if k.startswith("rejected_")):
        typer.echo(f"  discarded {counts[key]:>4}  {key.removeprefix('rejected_')}")
    for failure in report.failures:
        typer.secho(f"  warning: {failure}", fg=typer.colors.YELLOW, err=True)

    before, after = report.relevance_before_selection, report.relevance_after_selection
    typer.echo("")
    typer.echo(
        f"Eligible after the relevance gate (min {report.minimum_relevance}): "
        f"{before.get('direct', 0)} direct, {before.get('adjacent', 0)} adjacent."
    )
    typer.echo(
        f"Shortlist: {kept} of a requested {limit} - "
        f"{after.get('direct', 0)} direct, {after.get('adjacent', 0)} adjacent."
    )

    typer.echo("")
    for candidate in outcome.candidates.candidates:
        rank = candidate.discovery_rank
        label = rank.relevance_class.value if rank else "?"
        relevance = rank.relevance_score if rank else 0.0
        quality = rank.quality_score if rank else 0.0
        typer.echo(
            f"  {label:<8} rel={relevance:.2f} q={quality:.2f}  "
            f"{candidate.company_id:<26}  {candidate.website or ''}"
        )

    if report.shortfall:
        typer.secho(
            f"\nShortfall: {report.shortfall} place(s) unfilled. Not padded with "
            "off-topic candidates - see source-report.json.",
            fg=typer.colors.YELLOW,
        )

    typer.echo("")
    typer.echo(f"Wrote {outcome.candidates_path} and {outcome.report_path}")

    if kept == 0:
        typer.secho(
            "vc-scout source: no candidates survived discovery; see the sourcing report.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)


@app.command()
def enrich(
    run_id: str = _RUN_ID,
    runs_root: Path = _RUNS_ROOT,
    max_extra_pages: int = typer.Option(
        MAX_EXTRA_PAGES,
        "--max-extra-pages",
        min=0,
        max=MAX_EXTRA_PAGES,
        help="Internal pages fetched per company, beyond the homepage.",
    ),
    force: bool = typer.Option(False, "--force", help="Overwrite existing enrichment output."),
) -> None:
    """Fetch public company pages and write extracted/."""
    try:
        store = RunStore(run_id, runs_root=runs_root)
    except StoreError as exc:
        typer.secho(f"vc-scout enrich: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    if not store.candidates_path().exists():
        typer.secho(
            f"vc-scout enrich: run {run_id!r} has no candidates.json. Run `vc-scout source` first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    existing = store.extracted_company_ids()
    if (existing or store.enrichment_report_path().exists()) and not force:
        typer.secho(
            f"vc-scout enrich: run {run_id!r} already has enrichment output "
            f"({len(existing)} bundle(s)). Pass --force to re-fetch.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        with SafeFetcher() as fetcher:
            outcome = run_enrich(store=store, fetcher=fetcher, max_extra_pages=max_extra_pages)
    except StoreError as exc:
        typer.secho(f"vc-scout enrich: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    _report_enrich(outcome)


def _report_enrich(outcome: EnrichOutcome) -> None:
    """Print what was read and what could not be. Never exits non-zero for thin sites."""
    report = outcome.report
    counts = report.counts

    typer.echo(
        f"Enriched {counts.get('candidates', 0)} candidate(s): "
        f"{counts.get('success', 0)} complete, {counts.get('partial', 0)} partial, "
        f"{counts.get('failed', 0)} with no readable page."
    )
    typer.echo(
        f"Pages: {counts.get('pages_extracted', 0)} extracted of "
        f"{counts.get('pages_attempted', 0)} attempted "
        f"({counts.get('pages_deduplicated', 0)} deduplicated, "
        f"{counts.get('chars_extracted', 0):,} characters)."
    )
    for category, total in sorted(report.failures_by_category.items()):
        typer.echo(f"  failed {total:>4}  {category}")

    typer.echo("")
    for row in report.candidates:
        marker = {"success": " ", "partial": "~", "failed": "!"}[row.status.value]
        typer.echo(
            f"  {marker} {row.company_id:<26} {row.pages_extracted} page(s), "
            f"{row.chars_extracted:>6} chars  {row.website or '(no website)'}"
        )

    blind = [row.company_id for row in report.candidates if row.pages_extracted == 0]
    if blind:
        typer.secho(
            f"\n{len(blind)} candidate(s) have no readable pages and are retained with "
            "missing evidence: " + ", ".join(blind),
            fg=typer.colors.YELLOW,
        )

    typer.echo("")
    typer.echo(f"Wrote extracted/ and {outcome.report_path}")


@app.command()
def analyze(
    run_id: str = _RUN_ID,
    runs_root: Path = _RUNS_ROOT,
    evidence_only: bool = typer.Option(
        False, "--evidence-only", help="Run source-grounded evidence extraction and stop."
    ),
    provider: str = typer.Option(
        "anthropic", "--provider", help="LLM provider: anthropic, or fake for offline replay."
    ),
    model: str | None = typer.Option(
        None, "--model", help=f"Model identifier. Defaults to ${MODEL_ENV} or {DEFAULT_MODEL}."
    ),
    company_id: str | None = typer.Option(
        None,
        "--company-id",
        help="Analyse only this candidate, leaving every other analysis untouched.",
    ),
    effort: str = typer.Option(DEFAULT_EFFORT, "--effort", help="Model effort level."),
    max_tokens: int = typer.Option(DEFAULT_MAX_TOKENS, "--max-tokens", min=1024),
    force: bool = typer.Option(
        False, "--force", help="Overwrite existing evidence or analysis output."
    ),
) -> None:
    """Extract evidence, score against the rubric and apply the recommendation policy."""
    try:
        store = RunStore(run_id, runs_root=runs_root)
    except StoreError as exc:
        typer.secho(f"vc-scout analyze: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    if not store.candidates_path().exists():
        typer.secho(
            f"vc-scout analyze: run {run_id!r} has no candidates.json. Run `vc-scout source` "
            "and `vc-scout enrich` first.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    if evidence_only:
        existing = store.evidence_company_ids()
        if (existing or store.evidence_report_path().exists()) and not force:
            typer.secho(
                f"vc-scout analyze: run {run_id!r} already has evidence output "
                f"({len(existing)} dossier(s)). Pass --force to re-extract.",
                fg=typer.colors.YELLOW,
                err=True,
            )
            raise typer.Exit(code=1)
    else:
        if not store.evidence_company_ids():
            typer.secho(
                f"vc-scout analyze: run {run_id!r} has no evidence dossiers. Run "
                "`vc-scout analyze --evidence-only` first.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
        analysed = store.analysis_company_ids()
        if company_id is not None:
            # Only this candidate's analysis would be replaced, so only it gates the guard.
            analysed = [cid for cid in analysed if cid == company_id]
        if (
            analysed or (company_id is None and store.analysis_report_path().exists())
        ) and not force:
            typer.secho(
                f"vc-scout analyze: run {run_id!r} already has analysis output "
                f"({len(analysed)} analysis file(s)). Pass --force to re-analyse.",
                fg=typer.colors.YELLOW,
                err=True,
            )
            raise typer.Exit(code=1)

    resolved_model = model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL
    llm: LlmProvider
    if provider == "fake":
        llm = FakeProvider()
    elif provider == "anthropic":
        llm = AnthropicProvider()
        if not llm.api_key_present:
            typer.secho(
                f"vc-scout analyze: {API_KEY_ENV} is not set. Export it, or use "
                "--provider fake for an offline run.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(code=1)
    else:
        typer.secho(
            f"vc-scout analyze: unknown provider {provider!r}. Use anthropic or fake.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=1)

    config = ModelConfig(
        model=resolved_model,
        max_tokens=max_tokens,
        effort=effort,
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
    )
    if evidence_only:
        _report_evidence(run_evidence(store=store, provider=llm, config=config))
        return

    try:
        outcome = run_analysis(store=store, provider=llm, config=config, only_company_id=company_id)
    except UnknownCandidateError as exc:
        # Raised before any provider call, so a mistyped id never costs a request.
        typer.secho(f"vc-scout analyze: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    _report_analysis(outcome)


def _report_evidence(outcome: EvidenceStageOutcome) -> None:
    """Print what was extracted and what failed. Thin evidence is not a run failure."""
    report = outcome.report
    counts = report.counts

    typer.echo(
        f"Extracted evidence for {counts.get('succeeded', 0)} of "
        f"{counts.get('candidates', 0)} candidate(s) using {report.provider}/{report.model} "
        f"({report.prompt_version})."
    )
    typer.echo(
        f"Claims: {counts.get('claims', 0)}  unknowns: {counts.get('unknowns', 0)}  "
        f"conflicts: {counts.get('conflicts', 0)}  retried: {counts.get('retried', 0)}  "
        f"tokens in/out: {counts.get('input_tokens', 0):,}/{counts.get('output_tokens', 0):,}"
    )
    for category, total in sorted(report.failures_by_category.items()):
        typer.echo(f"  failed {total:>4}  {category}")

    typer.echo("")
    for row in report.candidates:
        marker = " " if row.succeeded else "!"
        site = "" if row.website_available else "  (no website evidence)"
        typer.echo(
            f"  {marker} {row.company_id:<26} {row.claims:>3} claims, "
            f"{row.unknowns:>2} unknowns, {row.sources_supplied} source(s){site}"
        )

    blind = [row.company_id for row in report.candidates if not row.succeeded]
    if blind:
        typer.secho(
            f"\n{len(blind)} candidate(s) produced no dossier and are retained with a "
            "recorded failure: " + ", ".join(blind),
            fg=typer.colors.YELLOW,
        )

    typer.echo("")
    written = counts.get("succeeded", 0)
    if written:
        typer.echo(f"Wrote {written} dossier(s) to evidence/, llm/ and {outcome.report_path}")
    else:
        typer.secho(
            f"No dossiers were produced. Wrote llm/ attempt artifacts and "
            f"{outcome.report_path}, which records why each candidate failed.",
            fg=typer.colors.RED,
            err=True,
        )


@app.command()
def recommend(
    run_id: str = _RUN_ID,
    runs_root: Path = _RUNS_ROOT,
    force: bool = typer.Option(
        False, "--force", help="Overwrite existing memos, ranking and report."
    ),
) -> None:
    """Render partner-ready memos and the portfolio ranking from the stored artifacts.

    Offline and deterministic: no provider is selected, no API key is required, and the
    same artifacts always produce the same bytes.
    """
    try:
        store = RunStore(run_id, runs_root=runs_root)
    except StoreError as exc:
        typer.secho(f"vc-scout recommend: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    existing = store.memo_company_ids()
    if (
        existing or store.ranking_path().exists() or store.recommendation_report_path().exists()
    ) and not force:
        typer.secho(
            f"vc-scout recommend: run {run_id!r} already has rendered output "
            f"({len(existing)} memo(s)). Pass --force to re-render.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        outcome = run_recommend(store=store)
    except MissingArtifactError as exc:
        typer.secho(f"vc-scout recommend: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    _report_recommend(outcome)


@app.command()
def render(
    run_id: str = _RUN_ID,
    runs_root: Path = _RUNS_ROOT,
    force: bool = typer.Option(
        False, "--force", help="Overwrite existing memos, ranking and report."
    ),
) -> None:
    """Deprecated alias for `recommend`, kept because the delivered CLI declared it."""
    typer.secho(
        "vc-scout render: this command is now `vc-scout recommend`. Running it.",
        fg=typer.colors.YELLOW,
        err=True,
    )
    recommend(run_id=run_id, runs_root=runs_root, force=force)


def _report_recommend(outcome: RecommendStageOutcome) -> None:
    """Print the shortlist in the order the ranking presents it."""
    report = outcome.report

    typer.echo(
        f"Rendered {report.memos_written} of {report.candidate_count} candidate memo(s) "
        f"using template {report.template_version}."
    )
    typer.echo(
        "Recommendations: "
        + "  ".join(f"{name}={total}" for name, total in sorted(report.recommendations.items()))
        + f"   model/policy disagreements: {report.model_policy_disagreements}"
    )
    if report.score_range:
        typer.echo(
            f"Scores {report.score_range['min']}-{report.score_range['max']}/100.  "
            f"Sources cited: {report.referenced_sources}.  "
            f"Confidence: "
            + "  ".join(f"{k}={v}" for k, v in sorted(report.confidence_counts.items()))
        )
    for guardrail, total in sorted(report.guardrail_counts.items()):
        typer.echo(f"  guardrail {total:>3}  {guardrail}")

    typer.echo("")
    for rank, memo in enumerate(report.memos, start=1):
        reach = "" if memo.maximum_achievable_score >= TAKE_A_MEETING_AT else " -"
        typer.echo(
            f"  {rank:>2}. {memo.company_id:<26} {memo.decision.value:<15} "
            f"{memo.total_score:>3}/100  max={memo.maximum_achievable_score:>3}{reach}  "
            f"{memo.words:>3} words, {memo.sources_referenced} source(s)"
        )

    if report.candidates_with_meeting_unreachable:
        typer.secho(
            f"\n  - the take-a-meeting band was unreachable for "
            f"{report.candidates_with_meeting_unreachable} of {report.memos_written} "
            "candidate(s) under their recorded assessment statuses.",
            fg=typer.colors.YELLOW,
        )
    if report.missing_source_metadata:
        typer.secho(
            f"  ! {report.missing_source_metadata} cited source(s) have no recorded URL and "
            "render with their internal identifier.",
            fg=typer.colors.YELLOW,
        )
    for warning in report.warnings:
        typer.secho(f"  ! {warning}", fg=typer.colors.YELLOW)
    for failure in report.failures:
        typer.secho(f"  ! {failure.company_id}: {failure.reason}", fg=typer.colors.YELLOW, err=True)

    typer.echo("")
    typer.echo(
        f"Wrote {report.memos_written} memo(s) to memos/, plus {outcome.ranking_path} and "
        f"{outcome.report_path}"
    )


@app.command("build-ui")
def build_ui(
    run_id: str = _RUN_ID,
    runs_root: Path = _RUNS_ROOT,
    force: bool = typer.Option(False, "--force", help="Replace an existing generated site."),
) -> None:
    """Generate the static read-only research site from the stored artifacts.

    Offline and deterministic: no provider is selected, no API key is required, and the
    same artifacts always produce the same bytes.
    """
    try:
        store = RunStore(run_id, runs_root=runs_root)
    except StoreError as exc:
        typer.secho(f"vc-scout build-ui: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    if store.site_dir.exists() and any(store.site_dir.iterdir()) and not force:
        typer.secho(
            f"vc-scout build-ui: run {run_id!r} already has a generated site at "
            f"{store.relative(store.site_dir)}/. Pass --force to rebuild it.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(code=1)

    try:
        outcome = run_build_ui(store=store, force=force)
    except UiArtifactError as exc:
        typer.secho(f"vc-scout build-ui: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc
    _report_build_ui(outcome, store)


def _report_build_ui(outcome: UiStageOutcome, store: RunStore) -> None:
    """Print what was generated and how to look at it."""
    report = outcome.report

    typer.echo(
        f"Generated {report.pages_written} page(s) for {report.candidate_count} candidate(s) "
        f"using template {report.template_version}."
    )
    typer.echo(
        "Recommendations: "
        + "  ".join(f"{name}={total}" for name, total in sorted(report.recommendations.items()))
        + "   confidence: "
        + "  ".join(f"{name}={total}" for name, total in sorted(report.confidence_counts.items()))
        + f"   sources cited: {report.sources_cited}"
    )
    for path in report.removed_paths:
        typer.secho(f"  - removed stale page {path}", fg=typer.colors.YELLOW)
    for warning in report.warnings:
        typer.secho(f"  ! {warning}", fg=typer.colors.YELLOW)
    for failure in report.failures:
        typer.secho(f"  ! {failure.company_id}: {failure.reason}", fg=typer.colors.YELLOW, err=True)

    typer.echo("")
    typer.echo(f"Wrote {outcome.site_dir}/ ({outcome.index_path}, {outcome.report_path})")
    typer.echo("Preview it with:")
    typer.echo(f"  python3 -m http.server 8000 --directory {store.site_dir}")


@app.command("build-site")
def build_site(
    run_id: str = _RUN_ID,
    runs_root: Path = _RUNS_ROOT,
    force: bool = typer.Option(False, "--force", help="Replace an existing generated site."),
) -> None:
    """Deprecated alias for `build-ui`, kept because the delivered CLI declared it."""
    typer.secho(
        "vc-scout build-site: this command is now `vc-scout build-ui`. Running it.",
        fg=typer.colors.YELLOW,
        err=True,
    )
    build_ui(run_id=run_id, runs_root=runs_root, force=force)


@app.command()
def serve(
    run_id: str = _RUN_ID,
    runs_root: Path = _RUNS_ROOT,
    port: int = typer.Option(8765, "--port", min=1, max=65535),
    host: str = typer.Option("127.0.0.1", "--host", help="Interface to bind. Loopback only."),
) -> None:
    """Serve a generated site locally, read-only."""
    _placeholder("serve", "stage 8", "serve site/ over http.server bound to loopback")


@app.command()
def run(
    query: str = typer.Option(..., "--query", help="Thesis-relevant search query."),
    run_id: str = _RUN_ID,
    limit: int = typer.Option(
        DEFAULT_LIMIT,
        "--limit",
        min=MIN_RUN_LIMIT,
        max=MAX_RUN_LIMIT,
        help=f"Candidates to shortlist, {MIN_RUN_LIMIT}-{MAX_RUN_LIMIT}.",
    ),
    provider: str = typer.Option(
        "anthropic", "--provider", help="LLM provider: anthropic, or fake for offline replay."
    ),
    model: str | None = typer.Option(
        None, "--model", help=f"Model identifier. Defaults to ${MODEL_ENV} or {DEFAULT_MODEL}."
    ),
    effort: str = typer.Option(DEFAULT_EFFORT, "--effort", help="Model effort level."),
    runs_root: Path = _RUNS_ROOT,
    max_extra_pages: int = typer.Option(
        MAX_EXTRA_PAGES,
        "--max-extra-pages",
        min=0,
        max=MAX_EXTRA_PAGES,
        help="Internal pages fetched per company, beyond the homepage.",
    ),
    max_tokens: int = typer.Option(DEFAULT_MAX_TOKENS, "--max-tokens", min=1024),
    force_stage: list[str] = _FORCE_STAGE,
    stop_after: str | None = typer.Option(
        None,
        "--stop-after",
        help="Stop once this stage finishes. Useful for debugging and walkthroughs.",
    ),
) -> None:
    """Run the whole pipeline: source, enrich, extract evidence, analyse, write up, publish.

    Stages whose artifacts are already current are resumed rather than repeated, so running
    this twice on a finished run makes no network and no provider call. Pass --force-stage
    to rebuild one stage and everything derived from it.
    """
    plan, store = _plan_run(
        query=query,
        limit=limit,
        run_id=run_id,
        runs_root=runs_root,
        provider=provider,
        model=model,
        effort=effort,
        max_extra_pages=max_extra_pages,
        max_tokens=max_tokens,
        force_stage=force_stage,
        stop_after=stop_after,
    )
    llm = _provider_for(provider, command="run")
    _execute_pipeline(store=store, plan=plan, llm=llm)


@app.command("recover-analysis")
def recover_analysis(
    run_id: str = _RUN_ID,
    runs_root: Path = _RUNS_ROOT,
    provider: str = typer.Option(
        "anthropic", "--provider", help="LLM provider: anthropic, or fake for offline replay."
    ),
    model: str | None = typer.Option(
        None, "--model", help=f"Model identifier. Defaults to ${MODEL_ENV} or {DEFAULT_MODEL}."
    ),
    effort: str = typer.Option(DEFAULT_EFFORT, "--effort", help="Model effort level."),
    max_tokens: int = typer.Option(DEFAULT_MAX_TOKENS, "--max-tokens", min=1024),
    company_id: list[str] = _RECOVER_COMPANY_ID,
) -> None:
    """Retry only the candidates a completed analysis run failed on, and rebuild downstream.

    Every successful analysis is left byte-identical. The failed candidates get the normal
    two attempts, the results are merged back into the full report with its candidate order
    and every total recomputed, and the memos and the site are rebuilt offline if anything
    recovered.
    """
    try:
        store = RunStore(run_id, runs_root=runs_root)
    except StoreError as exc:
        _fail("recover-analysis", str(exc))
    if provider not in ("anthropic", "fake"):
        _fail("recover-analysis", f"unknown provider {provider!r}. Use anthropic or fake.")

    # Read and plan before building a provider, so a refusal never depends on a credential.
    try:
        existing = store.read_analysis_report()
        targets = plan_recovery(existing, only=list(company_id))
    except (RecoveryError, StoreError, ValueError) as exc:
        _fail("recover-analysis", str(exc))

    if not targets:
        typer.echo(
            f"Nothing to recover: all {len(existing.candidates)} candidate(s) in "
            f"{run_id!r} already have a successful analysis. No provider call was made."
        )
        return

    typer.echo(
        f"Recovering {len(targets)} failed candidate(s) of {len(existing.candidates)}: "
        + ", ".join(targets)
    )
    typer.echo(
        f"  up to {MAX_ATTEMPTS} attempt(s) each, so at most "
        f"{len(targets) * MAX_ATTEMPTS} request(s). Every other analysis is left untouched."
    )

    llm = _provider_for(provider, command="recover-analysis")
    config = ModelConfig(
        model=model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL,
        max_tokens=max_tokens,
        effort=effort,
        timeout_seconds=DEFAULT_TIMEOUT_SECONDS,
    )
    try:
        outcome = recover_analyses(store=store, provider=llm, config=config)
    except RecoveryError as exc:
        _fail("recover-analysis", str(exc))
    _report_recovery(outcome, store)


def _report_recovery(outcome: RecoveryOutcome, store: RunStore) -> None:
    """Print what recovered, what did not, and what was rebuilt from it."""
    report = outcome.report
    typer.echo("")
    for company_id in outcome.attempted:
        row = next(row for row in report.candidates if row.company_id == company_id)
        rounds = [a for a in row.attempts if a.recovery_round == outcome.recovery_round]
        if row.succeeded:
            typer.secho(
                f"  recovered  {company_id:<26} {row.total_score:>3}/100  "
                f"{row.decision.value if row.decision else '?':<15} "
                f"({len(rounds)} attempt(s) this round)",
                fg=typer.colors.GREEN,
            )
        else:
            reason = row.error_category.value if row.error_category else "unknown"
            typer.secho(
                f"  failed     {company_id:<26} {reason} ({len(rounds)} attempt(s) this round)",
                fg=typer.colors.YELLOW,
            )

    counts = report.counts
    typer.echo("")
    typer.echo(
        f"Analyses: {counts.get('succeeded', 0)} of {counts.get('candidates', 0)} succeeded"
        + (f", {len(outcome.still_failed)} still failing" if outcome.still_failed else "")
        + "."
    )
    if not outcome.verified:
        typer.secho(
            "  ! the merged report claims a success whose analysis file does not load. "
            "Downstream stages were not rebuilt.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)

    analysis_stage = StageRun(
        stage=PipelineStage.ANALYSIS,
        status=(
            PipelineStageStatus.PARTIAL if outcome.still_failed else PipelineStageStatus.COMPLETED
        ),
        decision=(
            f"recovery round {outcome.recovery_round}: recovered "
            f"{len(outcome.recovered)} of {len(outcome.attempted)} failed candidate(s)"
        ),
        candidates_in=counts.get("candidates", 0),
        candidates_out=counts.get("succeeded", 0),
        failures=counts.get("failed", 0),
        artifacts=[outcome.report_path],
        upstream_fingerprint=report.upstream_fingerprint,
    )
    rebuild = apply_recovery(store=store, analysis_stage=analysis_stage, rebuild=outcome.changed)
    if rebuild.rebuilt:
        typer.echo(f"Rebuilt {rebuild.memos} memo(s) and {rebuild.pages} page(s).")
    else:
        typer.secho(
            "Nothing recovered, so the memos and the site were left as they were.",
            fg=typer.colors.YELLOW,
        )

    typer.echo("")
    typer.echo(f"Report:  {outcome.report_path}")
    if store.ranking_path().is_file():
        typer.echo(f"Ranking: {store.relative(store.ranking_path())}")
    if (store.site_dir / "index.html").is_file():
        typer.echo(f"Site:    {store.relative(store.site_dir)}/")
        typer.echo("")
        typer.echo("Preview the site with:")
        typer.echo(f"  python3 -m http.server 8000 --directory {store.site_dir}")


@app.command()
def demo(
    run_id: str = typer.Option("offline-demo", "--run-id", help="Run directory to write."),
    runs_root: Path = _RUNS_ROOT,
    force: bool = typer.Option(False, "--force", help="Rebuild every stage from scratch."),
) -> None:
    """Run the whole pipeline offline against committed fixtures, with no API key.

    Same orchestrator, same stages, same HTTP and Algolia clients - only the transport
    underneath them serves committed files instead of the internet, and the provider is the
    deterministic fake. It produces real memos and a real site.
    """
    try:
        store = RunStore(run_id, runs_root=runs_root)
    except StoreError as exc:
        typer.secho(f"vc-scout demo: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    plan = Plan(
        query=DEMO_QUERY,
        limit=DEMO_LIMIT,
        provider_name="fake",
        model="fake-model-1",
        effort=DEFAULT_EFFORT,
        forced=frozenset(STAGE_ORDER) if force else frozenset(),
    )
    typer.echo(
        f"Offline demo: {DEMO_LIMIT} candidates from committed fixtures, deterministic "
        "provider, no credential required."
    )
    _execute_pipeline(
        store=store,
        plan=plan,
        llm=FakeProvider(),
        client_factory=demo_client,
        fetcher_factory=demo_fetcher,
    )


@app.command("export-demo")
def export_demo_command(
    run_id: str = _RUN_ID,
    runs_root: Path = _RUNS_ROOT,
    destination: Path = _DESTINATION,
    force: bool = typer.Option(False, "--force", help="Replace an existing export."),
) -> None:
    """Assemble a reviewer-ready demo/ directory from a completed run.

    Offline. Copies the site, the memos, the ranking, every validated artifact and one AI
    call end to end. No raw HTML, no credentials, no absolute paths - the export refuses to
    write rather than ship any of them.
    """
    try:
        store = RunStore(run_id, runs_root=runs_root)
    except StoreError as exc:
        typer.secho(f"vc-scout export-demo: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    try:
        result = export_demo(store=store, destination=destination, force=force)
    except (ExportError, StoreError) as exc:
        typer.secho(f"vc-scout export-demo: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    typer.echo(
        f"Exported {len(result.files)} file(s) to {destination}/: "
        f"{result.memos} memo(s), {result.company_pages} company page(s)."
    )
    if result.trace_company_id:
        typer.echo(
            f"AI trace: {result.trace_company_id} - the richest successful analysis. "
            "The rule is in ai-trace/README.md."
        )
    else:
        typer.secho(
            "No AI trace: this run produced no successful analysis.",
            fg=typer.colors.YELLOW,
        )
    typer.echo("")
    typer.echo("Preview it with:")
    typer.echo(f"  python3 -m http.server 8000 --directory {destination}/site")


def _plan_run(
    *,
    query: str,
    limit: int,
    run_id: str,
    runs_root: Path,
    provider: str,
    model: str | None,
    effort: str,
    max_extra_pages: int,
    max_tokens: int,
    force_stage: list[str],
    stop_after: str | None,
) -> tuple[Plan, RunStore]:
    """Validate every option before a run directory is created or a call is made.

    A mistyped stage name must not cost a directory, let alone a request.
    """
    if not query.strip():
        _fail("run", "--query must not be empty.")
    try:
        forced = frozenset(PipelineStage(name) for name in force_stage)
    except ValueError:
        _fail(
            "run",
            f"--force-stage must be one of: {', '.join(s.value for s in STAGE_ORDER)}.",
        )
    try:
        stop = PipelineStage(stop_after) if stop_after else None
    except ValueError:
        _fail(
            "run",
            f"--stop-after must be one of: {', '.join(s.value for s in STAGE_ORDER)}.",
        )
    if provider not in ("anthropic", "fake"):
        _fail("run", f"unknown provider {provider!r}. Use anthropic or fake.")

    try:
        store = RunStore(run_id, runs_root=runs_root)
    except StoreError as exc:
        _fail("run", str(exc))

    plan = Plan(
        query=query.strip(),
        limit=limit,
        provider_name=provider,
        model=model or os.environ.get(MODEL_ENV) or DEFAULT_MODEL,
        effort=effort,
        max_extra_pages=max_extra_pages,
        max_tokens=max_tokens,
        forced=forced,
        stop_after=stop,
    )
    return plan, store


def _fail(command: str, message: str) -> NoReturn:
    typer.secho(f"vc-scout {command}: {message}", fg=typer.colors.RED, err=True)
    raise typer.Exit(code=1)


def _provider_for(provider: str, *, command: str) -> LlmProvider:
    """Build the provider, refusing early if it cannot possibly work."""
    if provider == "fake":
        return FakeProvider()
    llm = AnthropicProvider()
    if not llm.api_key_present:
        _fail(
            command,
            f"{API_KEY_ENV} is not set. Export it, or use --provider fake for an offline run.",
        )
    return llm


def _execute_pipeline(
    *,
    store: RunStore,
    plan: Plan,
    llm: LlmProvider,
    client_factory: Callable[[], HnAlgoliaClient] = HnAlgoliaClient,
    fetcher_factory: Callable[[], SafeFetcher] = SafeFetcher,
) -> None:
    """Run the pipeline, printing a stage timeline as it goes."""
    typer.echo(
        f"Run {store.run_id!r}: {plan.provider_name}/{plan.model} (effort {plan.effort}), "
        f"limit {plan.limit}."
    )
    if plan.forced:
        typer.echo(
            "Forcing: "
            + ", ".join(sorted(stage.value for stage in plan.forced))
            + " (and everything downstream)"
        )
    typer.echo("")

    def announce(record: StageRun) -> None:
        mark = {
            PipelineStageStatus.COMPLETED: "ok  ",
            PipelineStageStatus.PARTIAL: "part",
            PipelineStageStatus.FAILED: "FAIL",
            PipelineStageStatus.SKIPPED: "skip",
        }[record.status]
        colour = (
            typer.colors.RED
            if record.status is PipelineStageStatus.FAILED
            else typer.colors.YELLOW
            if record.status is PipelineStageStatus.PARTIAL
            else None
        )
        how = "resumed" if record.resumed else "ran    "
        typer.secho(
            f"  [{mark}] {record.stage.value:<10} {how} {record.duration_seconds:>6.2f}s  "
            f"{record.decision or ''}",
            fg=colour,
        )

    try:
        result = run_pipeline(
            store=store,
            plan=plan,
            provider=llm,
            client_factory=client_factory,
            fetcher_factory=fetcher_factory,
            on_stage=announce,
        )
    except PipelineAbortedError as exc:
        typer.secho(f"\nvc-scout run: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(code=1) from exc

    _report_run(result, store)


def _report_run(result: PipelineResult, store: RunStore) -> None:
    """The final handoff: what exists now, and how to look at it."""
    report = result.report
    typer.echo("")
    typer.echo(
        f"Finished in {report.duration_seconds:.1f}s. "
        f"{report.candidate_flow.get('source_out', 0)} candidate(s) discovered."
    )
    if report.recommendations:
        typer.echo(
            "Recommendations: "
            + "  ".join(f"{name}={total}" for name, total in sorted(report.recommendations.items()))
        )
    if report.token_usage:
        typer.echo(
            "Tokens: "
            + "  ".join(f"{name}={total:,}" for name, total in sorted(report.token_usage.items()))
        )
    for name, total in sorted(report.failure_summary.items()):
        typer.secho(f"  {total:>3}  {name}", fg=typer.colors.YELLOW)

    typer.echo("")
    if report.ranking_path:
        typer.echo(f"Ranking: {store.relative(store.ranking_path())}")
    if report.memos_path:
        typer.echo(f"Memos:   {store.relative(store.resolve('memos'))}/")
    if report.site_path:
        typer.echo(f"Site:    {store.relative(store.site_dir)}/")
    typer.echo(f"Report:  {result.report_path}")

    if report.site_path:
        typer.echo("")
        typer.echo("Preview the site with:")
        typer.echo(f"  python3 -m http.server 8000 --directory {store.site_dir}")

    failed = [
        record.stage.value
        for record in report.stages
        if record.status is PipelineStageStatus.FAILED
    ]
    if failed:
        typer.secho(
            f"\nStage(s) that failed: {', '.join(failed)}. See {result.report_path}.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(code=2)


@app.command()
def config() -> None:
    """Show the active scoring configuration and policy thresholds."""
    typer.echo(f"vc-scout {__version__}")
    typer.echo(f"rubric version: {RUBRIC_VERSION}")
    typer.echo(f"policy version: {POLICY_VERSION}")
    typer.echo("")
    typer.echo("Rubric:")
    for spec in RUBRIC:
        typer.echo(f"  {spec.max_points:>3}  {spec.key.value:<14}  {spec.title}")
    typer.echo(f"  {sum(spec.max_points for spec in RUBRIC):>3}  total")
    typer.echo("")
    typer.echo("Recommendation bands:")
    typer.echo(f"  {TAKE_A_MEETING_AT}-100  take_a_meeting")
    typer.echo(f"  {WATCH_AT}-{TAKE_A_MEETING_AT - 1}   watch")
    typer.echo(f"  0-{WATCH_AT - 1}    pass")
    typer.echo("")
    typer.echo("Low research confidence caps the recommendation at watch.")


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":  # pragma: no cover
    main()


def _report_analysis(outcome: AnalysisStageOutcome) -> None:
    """Print the scored shortlist and the policy's calls. Thin evidence is not a failure."""
    report = outcome.report
    counts = report.counts

    if report.filtered_to:
        typer.secho(
            f"Filtered run: only {report.filtered_to!r} was analysed. Every other "
            "candidate's analysis was left untouched.",
            fg=typer.colors.YELLOW,
        )
    typer.echo(
        f"Analysed {counts.get('succeeded', 0)} of {counts.get('candidates', 0)} candidate(s) "
        f"using {report.provider}/{report.model} ({report.prompt_version}, "
        f"{report.thesis_version}, policy {report.policy_version})."
    )
    typer.echo(
        "Recommendations: "
        + "  ".join(f"{name}={total}" for name, total in sorted(report.recommendations.items()))
        + f"   retried: {counts.get('retried', 0)}"
        + f"   model/policy disagreements: {counts.get('model_policy_disagreements', 0)}"
    )
    typer.echo(
        f"Tokens in/out: {counts.get('input_tokens', 0):,}/{counts.get('output_tokens', 0):,}"
    )
    for category, total in sorted(report.failures_by_category.items()):
        typer.echo(f"  failed {total:>4}  {category}")
    for guardrail, total in sorted(report.guardrails.items()):
        typer.echo(f"  guardrail {total:>3}  {guardrail}")

    typer.echo("")
    for row in sorted(report.candidates, key=lambda r: (-(r.total_score or -1), r.company_id)):
        if not row.succeeded:
            reason = row.error_category.value if row.error_category else "unknown"
            typer.secho(f"  ! {row.company_id:<26} no analysis ({reason})", fg=typer.colors.YELLOW)
            continue
        suggestion = row.model_suggested.value if row.model_suggested else "-"
        flag = "*" if row.model_disagreed else " "
        # `max` is the highest total this analysis could have reached under its own
        # statuses; a trailing dash marks a candidate for which the meeting band was
        # arithmetically out of reach on this evidence.
        reach = "" if row.meeting_reachable_by_statuses else " -"
        typer.echo(
            f"  {flag} {row.company_id:<26} {row.total_score:>3}/100  "
            f"{(row.decision.value if row.decision else '?'):<15} "
            f"conf={(row.confidence_level.value if row.confidence_level else '?'):<6} "
            f"na={row.not_assessable} max={row.maximum_achievable_score or 0:>3}{reach}"
            f"  model={suggestion}"
        )
    if counts.get("model_policy_disagreements"):
        typer.echo("\n  * the model suggested a different recommendation from the policy")
    unreachable = [
        row
        for row in report.candidates
        if row.succeeded and row.meeting_reachable_by_statuses is False
    ]
    if unreachable:
        typer.secho(
            f"  - the take-a-meeting band was unreachable for {len(unreachable)} of "
            f"{counts.get('succeeded', 0)} analysed candidate(s): under their recorded "
            "assessment statuses, the rubric ceilings cap the achievable total below 80.",
            fg=typer.colors.YELLOW,
        )

    typer.echo("")
    written = counts.get("succeeded", 0)
    # Name only what was actually produced. A run that analysed nothing must not report
    # having written analyses.
    if written:
        typer.echo(f"Wrote {written} analysis file(s) to analyses/, llm/ and {outcome.report_path}")
    else:
        typer.secho(
            f"No analyses were produced. Wrote llm/ attempt artifacts and "
            f"{outcome.report_path}, which records why each candidate failed.",
            fg=typer.colors.RED,
            err=True,
        )
