"""CLI surface.

The full command set is part of the delivered contract, so it is pinned by a test rather
than left to drift. Unimplemented commands must say so and exit non-zero - never exit 0
having done nothing.
"""

from __future__ import annotations

import pytest
from typer.testing import CliRunner

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
        ("source", ["--query", "q", "--run-id", "demo"]),
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
