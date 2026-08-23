"""Command-line interface.

Every command listed here is part of the delivered contract. In this stage the pipeline
commands are placeholders: they parse their arguments, report which implementation stage
owns them, and exit non-zero. They do not pretend to have done work.
"""

from __future__ import annotations

from pathlib import Path

import typer

from vc_scout import __version__
from vc_scout.config import DEFAULT_LIMIT
from vc_scout.net.hn import HnAlgoliaClient, HnError
from vc_scout.net.http import SafeFetcher
from vc_scout.policy import POLICY_VERSION, TAKE_A_MEETING_AT, WATCH_AT
from vc_scout.rubric import RUBRIC, RUBRIC_VERSION
from vc_scout.stages.enrich import MAX_EXTRA_PAGES, EnrichOutcome, run_enrich
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
    provider: str | None = typer.Option(None, "--provider", help="LLM provider to use."),
    model: str | None = typer.Option(None, "--model", help="Model identifier."),
    force: bool = typer.Option(False, "--force", help="Recompute even if artifacts exist."),
) -> None:
    """Extract evidence, score against the rubric and apply the recommendation policy."""
    _placeholder(
        "analyze",
        "stages 4-6",
        "extract cited evidence, score the rubric, then apply the deterministic policy "
        "to produce evidence/ and analyses/",
    )


@app.command()
def render(run_id: str = _RUN_ID, runs_root: Path = _RUNS_ROOT) -> None:
    """Render one-page Markdown memos and the ranking table."""
    _placeholder("render", "stage 7", "render memos/<company_id>.md and ranking.md")


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
