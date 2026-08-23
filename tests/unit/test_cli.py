"""CLI surface.

The full command set is part of the delivered contract, so it is pinned by a test rather
than left to drift. Unimplemented commands must say so and exit non-zero - never exit 0
having done nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.unit.hn_fixtures import QUERY, load_fixture, make_client
from tests.unit.web_fixtures import fetcher, html_response, load_html
from vc_scout import cli
from vc_scout.cli import NOT_IMPLEMENTED_EXIT, app

REQUIRED_COMMANDS = [
    "source",
    "enrich",
    "analyze",
    "render",
    "build-site",
    "serve",
    "run",
    "demo",
]

runner = CliRunner()


def test_help_lists_every_required_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    for command in REQUIRED_COMMANDS:
        assert command in result.output


@pytest.mark.parametrize("command", REQUIRED_COMMANDS)
def test_each_command_has_its_own_help(command: str) -> None:
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("analyze", ["--run-id", "demo"]),
        ("render", ["--run-id", "demo"]),
        ("build-site", ["--run-id", "demo"]),
        ("serve", ["--run-id", "demo"]),
        ("run", ["--query", "q", "--run-id", "demo"]),
        ("demo", []),
    ],
)
def test_placeholder_commands_report_honestly(command: str, args: list[str]) -> None:
    result = runner.invoke(app, [command, *args])
    assert result.exit_code == NOT_IMPLEMENTED_EXIT
    assert "not implemented yet" in result.output


def test_source_requires_a_query() -> None:
    assert runner.invoke(app, ["source", "--run-id", "demo"]).exit_code != 0


def test_config_reports_the_live_rubric_and_thresholds() -> None:
    result = runner.invoke(app, ["config"])
    assert result.exit_code == 0
    assert "pain_roi" in result.output
    assert "100  total" in result.output
    assert "80-100  take_a_meeting" in result.output
    assert "caps the recommendation at watch" in result.output


# -- source ------------------------------------------------------------------

CORPUS = (
    "intent-smb",
    "intent-customer-support",
    "intent-finance-accounting",
    "query-story",
    "query-show-hn",
)


@pytest.fixture
def fixture_hn(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the CLI's client factory at committed fixtures instead of the network."""
    responses = {label: load_fixture(label) for label in CORPUS}
    monkeypatch.setattr(cli, "HnAlgoliaClient", lambda: make_client(responses))


def test_source_writes_artifacts_and_summarises_the_funnel(
    fixture_hn: None, tmp_path: Path
) -> None:
    result = runner.invoke(
        app,
        [
            "source",
            "--query",
            QUERY,
            "--limit",
            "15",
            "--run-id",
            "source-test",
            "--runs-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "query variants" in result.output
    assert "discarded" in result.output
    assert "Eligible after the relevance gate" in result.output
    assert "Shortlist:" in result.output
    assert "candidates.json" in result.output
    assert "source-report.json" in result.output

    run_dir = tmp_path / "source-test"
    assert (run_dir / "candidates.json").exists()
    assert (run_dir / "source-report.json").exists()
    assert list((run_dir / "raw" / "hn").glob("*.json"))


def test_source_shows_the_relevance_class_of_every_shortlisted_candidate(
    fixture_hn: None, tmp_path: Path
) -> None:
    result = runner.invoke(
        app,
        ["source", "--query", QUERY, "--run-id", "r", "--runs-root", str(tmp_path)],
    )
    assert result.exit_code == 0, result.output
    assert "direct  " in result.output
    assert "rel=" in result.output and "q=" in result.output


def test_source_warns_about_a_shortfall_instead_of_padding(
    fixture_hn: None, tmp_path: Path
) -> None:
    result = runner.invoke(
        app,
        [
            "source",
            "--query",
            QUERY,
            "--limit",
            "15",
            "--run-id",
            "r",
            "--runs-root",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Shortfall:" in result.output
    assert "Not padded" in result.output


def test_source_refuses_to_clobber_an_existing_run_without_force(
    fixture_hn: None, tmp_path: Path
) -> None:
    args = ["source", "--query", QUERY, "--run-id", "source-test", "--runs-root", str(tmp_path)]
    assert runner.invoke(app, args).exit_code == 0

    repeat = runner.invoke(app, args)
    assert repeat.exit_code == 1
    assert "--force" in repeat.output

    assert runner.invoke(app, [*args, "--force"]).exit_code == 0


def test_source_rejects_an_unusable_run_id(fixture_hn: None, tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["source", "--query", QUERY, "--run-id", "../escape", "--runs-root", str(tmp_path)],
    )
    assert result.exit_code == 1
    assert "invalid run id" in result.output


def test_source_exits_non_zero_when_nothing_survives_discovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(cli, "HnAlgoliaClient", lambda: make_client({}))

    result = runner.invoke(
        app,
        ["source", "--query", QUERY, "--run-id", "source-test", "--runs-root", str(tmp_path)],
    )
    assert result.exit_code == 1
    assert "no candidates survived" in result.output
    # The artifacts are still written, so the empty run can be diagnosed.
    assert (tmp_path / "source-test" / "source-report.json").exists()


# -- enrich ------------------------------------------------------------------


@pytest.fixture
def fixture_web(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the CLI's fetcher factory at fixtures instead of the network."""
    routes = {
        "https://acme.example/": html_response(load_html("homepage")),
        "https://acme.example/product": html_response(load_html("pricing")),
    }
    monkeypatch.setattr(cli, "SafeFetcher", lambda: fetcher(routes))


def seeded_run(tmp_path: Path, *, website: str | None = "https://acme.example/") -> Path:
    """A run directory containing a single-candidate candidates.json."""
    from vc_scout.models.candidate import Candidate, CandidateSet
    from vc_scout.models.enums import SourceKind
    from vc_scout.models.source import SourceReference
    from vc_scout.store import RunStore

    store = RunStore("source-test", runs_root=tmp_path)
    store.ensure_root()
    hn = SourceReference.create("https://news.ycombinator.com/item?id=1", kind=SourceKind.HN_STORY)
    store.write_candidates(
        CandidateSet(
            run_id="source-test",
            query=QUERY,
            sources=[hn],
            candidates=[
                Candidate(
                    company_id="acme-ops",
                    name="Acme Ops",
                    source_ids=[hn.source_id],
                    website=website,
                )
            ],
        )
    )
    return store.root


def test_enrich_reads_pages_and_summarises(fixture_web: None, tmp_path: Path) -> None:
    run_dir = seeded_run(tmp_path)
    result = runner.invoke(app, ["enrich", "--run-id", "source-test", "--runs-root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "Enriched 1 candidate" in result.output
    assert "Pages:" in result.output
    assert "enrichment-report.json" in result.output

    assert (run_dir / "extracted" / "acme-ops.json").exists()
    assert (run_dir / "enrichment-report.json").exists()
    assert list((run_dir / "raw" / "web" / "acme-ops").glob("*.html"))


def test_enrich_requires_candidates_first(tmp_path: Path) -> None:
    result = runner.invoke(app, ["enrich", "--run-id", "missing", "--runs-root", str(tmp_path)])
    assert result.exit_code == 1
    assert "no candidates.json" in result.output


def test_enrich_requires_force_before_overwriting(fixture_web: None, tmp_path: Path) -> None:
    seeded_run(tmp_path)
    args = ["enrich", "--run-id", "source-test", "--runs-root", str(tmp_path)]
    assert runner.invoke(app, args).exit_code == 0

    repeat = runner.invoke(app, args)
    assert repeat.exit_code == 1
    assert "--force" in repeat.output

    assert runner.invoke(app, [*args, "--force"]).exit_code == 0


def test_enrich_succeeds_and_flags_a_candidate_with_no_readable_pages(
    fixture_web: None, tmp_path: Path
) -> None:
    """A blind candidate is reported, not treated as a run failure."""
    seeded_run(tmp_path, website="https://unreachable.example/")
    result = runner.invoke(app, ["enrich", "--run-id", "source-test", "--runs-root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "no readable pages" in result.output
    assert "acme-ops" in result.output


def test_enrich_rejects_an_unusable_run_id(tmp_path: Path) -> None:
    result = runner.invoke(app, ["enrich", "--run-id", "../escape", "--runs-root", str(tmp_path)])
    assert result.exit_code == 1
    assert "invalid run id" in result.output
