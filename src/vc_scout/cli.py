"""Command-line interface.

Every command listed here is part of the delivered contract. In this stage the pipeline
commands are placeholders: they parse their arguments, report which implementation stage
owns them, and exit non-zero. They do not pretend to have done work.
"""

from __future__ import annotations

import os
from pathlib import Path

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
from vc_scout.llm.anthropic import AnthropicProvider
from vc_scout.llm.fake import FakeProvider
from vc_scout.llm.provider import LlmProvider, ModelConfig
from vc_scout.net.hn import HnAlgoliaClient, HnError
from vc_scout.net.http import SafeFetcher
from vc_scout.policy import POLICY_VERSION, TAKE_A_MEETING_AT, WATCH_AT
from vc_scout.rubric import RUBRIC, RUBRIC_VERSION
from vc_scout.stages.analysis import (
    AnalysisStageOutcome,
    UnknownCandidateError,
    run_analysis,
)
from vc_scout.stages.enrich import MAX_EXTRA_PAGES, EnrichOutcome, run_enrich
from vc_scout.stages.evidence import EvidenceStageOutcome, run_evidence
from vc_scout.stages.recommend import (
    MissingArtifactError,
    RecommendStageOutcome,
    run_recommend,
)
from vc_scout.stages.source import DEFAULT_WINDOW_DAYS, SourceOutcome, run_source
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
_RUNS_ROOT = typer.Option(
    Path("outputs/runs"), "--runs-root", help="Root directory holding all runs."
)

#: Exit code used by a command that is declared but not yet implemented.
NOT_IMPLEMENTED_EXIT = 2


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


@app.command("build-site")
def build_site(run_id: str = _RUN_ID, runs_root: Path = _RUNS_ROOT) -> None:
    """Generate the static read-only research site."""
    _placeholder(
        "build-site",
        "stage 8",
        "generate site/index.html, site/companies/ and site/methodology.html",
    )


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
    limit: int = typer.Option(DEFAULT_LIMIT, "--limit", min=1, help="Maximum candidates to keep."),
    run_id: str = _RUN_ID,
    runs_root: Path = _RUNS_ROOT,
    provider: str | None = typer.Option(None, "--provider", help="LLM provider to use."),
    model: str | None = typer.Option(None, "--model", help="Model identifier."),
    strict: bool = typer.Option(False, "--strict", help="Fail the run if any company fails."),
) -> None:
    """Run the full pipeline end to end."""
    _placeholder("run", "stage 9", "execute source through build-site in one pass")


@app.command()
def demo(
    run_id: str = typer.Option("demo", "--run-id", help="Run directory to write."),
    runs_root: Path = _RUNS_ROOT,
    force: bool = typer.Option(False, "--force", help="Overwrite an existing demo run."),
) -> None:
    """Reproduce the committed demo run offline, with no API key."""
    _placeholder(
        "demo",
        "stage 9",
        "rebuild the committed demo run from fixtures using the deterministic fake provider",
    )


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
