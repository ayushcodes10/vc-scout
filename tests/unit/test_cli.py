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
        ("enrich", ["--run-id", "demo"]),
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
