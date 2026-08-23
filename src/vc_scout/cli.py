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
from vc_scout.policy import POLICY_VERSION, TAKE_A_MEETING_AT, WATCH_AT
from vc_scout.rubric import RUBRIC, RUBRIC_VERSION

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
    force: bool = typer.Option(False, "--force", help="Recompute even if artifacts exist."),
) -> None:
    """Discover candidate startups and write candidates.json."""
    _placeholder(
        "source",
        "stage 2",
        "search Hacker News via the Algolia API, dedupe candidates and persist "
        "raw/hn/ plus candidates.json",
    )


@app.command()
def enrich(
    run_id: str = _RUN_ID,
    runs_root: Path = _RUNS_ROOT,
    max_pages: int = typer.Option(4, "--max-pages", min=1, help="Pages fetched per company."),
    force: bool = typer.Option(False, "--force", help="Recompute even if artifacts exist."),
) -> None:
    """Fetch public company pages and write extracted/."""
    _placeholder(
        "enrich",
        "stage 3",
        "fetch each candidate's public website and HN thread, then persist raw/pages/ "
        "and extracted/<company_id>.json",
    )


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
