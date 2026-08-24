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
from vc_scout.store import RunStore

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


# -- analyze --evidence-only -------------------------------------------------


def evidence_run(tmp_path: Path) -> RunStore:
    from tests.unit.evidence_fixtures import seed_run

    store = RunStore("source-test", runs_root=tmp_path)
    seed_run(store)
    return store


@pytest.fixture
def fixture_llm(monkeypatch: pytest.MonkeyPatch) -> None:
    """Point the CLI's provider factory at the deterministic fake."""
    from vc_scout.llm.fake import FakeProvider

    monkeypatch.setattr(cli, "FakeProvider", FakeProvider)


def test_analyze_requires_evidence_before_it_will_score(tmp_path: Path) -> None:
    evidence_run(tmp_path)
    result = runner.invoke(
        app, ["analyze", "--run-id", "source-test", "--runs-root", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "no evidence dossiers" in result.output
    assert "--evidence-only" in result.output


def test_analyze_evidence_only_writes_artifacts(fixture_llm: None, tmp_path: Path) -> None:
    store = evidence_run(tmp_path)
    result = runner.invoke(
        app,
        [
            "analyze",
            "--run-id",
            "source-test",
            "--runs-root",
            str(tmp_path),
            "--evidence-only",
            "--provider",
            "fake",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Extracted evidence for" in result.output
    assert "evidence-report.json" in result.output

    assert store.evidence_report_path().exists()
    assert list(store.resolve("llm", "evidence-requests").glob("*.json"))
    assert list(store.resolve("llm", "evidence-responses").glob("*.json"))


def test_analyze_requires_candidates_first(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        ["analyze", "--run-id", "missing", "--runs-root", str(tmp_path), "--evidence-only"],
    )
    assert result.exit_code == 1
    assert "no candidates.json" in result.output


def test_analyze_requires_force_before_overwriting(fixture_llm: None, tmp_path: Path) -> None:
    evidence_run(tmp_path)
    args = [
        "analyze",
        "--run-id",
        "source-test",
        "--runs-root",
        str(tmp_path),
        "--evidence-only",
        "--provider",
        "fake",
    ]
    assert runner.invoke(app, args).exit_code == 0

    repeat = runner.invoke(app, args)
    assert repeat.exit_code == 1
    assert "--force" in repeat.output

    assert runner.invoke(app, [*args, "--force"]).exit_code == 0


def test_analyze_refuses_the_live_provider_without_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    evidence_run(tmp_path)
    result = runner.invoke(
        app,
        ["analyze", "--run-id", "source-test", "--runs-root", str(tmp_path), "--evidence-only"],
    )
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY is not set" in result.output
    assert "--provider fake" in result.output


def test_analyze_rejects_an_unknown_provider(tmp_path: Path) -> None:
    evidence_run(tmp_path)
    result = runner.invoke(
        app,
        [
            "analyze",
            "--run-id",
            "source-test",
            "--runs-root",
            str(tmp_path),
            "--evidence-only",
            "--provider",
            "openai",
        ],
    )
    assert result.exit_code == 1
    assert "unknown provider" in result.output


# -- analyze (scoring mode) --------------------------------------------------


def scored_run(tmp_path: Path):
    """A run with candidates and evidence dossiers, ready to analyse."""
    from tests.unit.analysis_fixtures import analysis_payload, dossier, seed_run

    store = RunStore("source-test", runs_root=tmp_path)
    bundle = dossier(claims=6)
    seed_run(store, [bundle])
    return store, bundle, analysis_payload(bundle)


def test_analyze_scores_and_recommends(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from vc_scout.llm.fake import FakeProvider

    store, _, payload = scored_run(tmp_path)
    monkeypatch.setattr(cli, "FakeProvider", lambda: FakeProvider([payload]))

    result = runner.invoke(
        app,
        [
            "analyze",
            "--run-id",
            "source-test",
            "--runs-root",
            str(tmp_path),
            "--provider",
            "fake",
            "--effort",
            "low",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Analysed 1 of 1 candidate" in result.output
    assert "Recommendations:" in result.output
    assert "analysis-report.json" in result.output

    assert store.analysis_report_path().exists()
    assert store.analysis_company_ids() == ["acme-ops"]
    assert list(store.resolve("llm", "analysis-requests").glob("*.json"))
    assert list(store.resolve("llm", "analysis-responses").glob("*.json"))


def test_analyze_requires_force_before_overwriting_an_analysis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from vc_scout.llm.fake import FakeProvider

    _, _, payload = scored_run(tmp_path)
    monkeypatch.setattr(cli, "FakeProvider", lambda: FakeProvider([payload, dict(payload)]))
    args = [
        "analyze",
        "--run-id",
        "source-test",
        "--runs-root",
        str(tmp_path),
        "--provider",
        "fake",
    ]
    assert runner.invoke(app, args).exit_code == 0

    repeat = runner.invoke(app, args)
    assert repeat.exit_code == 1
    assert "--force" in repeat.output
    assert runner.invoke(app, [*args, "--force"]).exit_code == 0


def test_evidence_only_mode_is_unaffected_by_the_scoring_mode(
    fixture_llm: None, tmp_path: Path
) -> None:
    """The existing --evidence-only behaviour must remain unchanged."""
    evidence_run(tmp_path)
    result = runner.invoke(
        app,
        [
            "analyze",
            "--run-id",
            "source-test",
            "--runs-root",
            str(tmp_path),
            "--evidence-only",
            "--provider",
            "fake",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Extracted evidence for" in result.output
    assert not (tmp_path / "source-test" / "analysis-report.json").exists()


def test_analyze_accepts_a_single_company_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tests.unit.analysis_fixtures import dossier
    from tests.unit.analysis_fixtures import seed_run as seed_analysis
    from vc_scout.llm.fake import FakeProvider

    store = RunStore("source-test", runs_root=tmp_path)
    seed_analysis(
        store, [dossier(company_id="co-00", claims=4), dossier(company_id="co-01", claims=4)]
    )
    monkeypatch.setattr(cli, "FakeProvider", FakeProvider)

    result = runner.invoke(
        app,
        [
            "analyze",
            "--run-id",
            "source-test",
            "--runs-root",
            str(tmp_path),
            "--provider",
            "fake",
            "--company-id",
            "co-01",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Filtered run: only 'co-01' was analysed" in result.output
    assert store.analysis_company_ids() == ["co-01"]


def test_analyze_rejects_an_unknown_company_id_before_any_call(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from tests.unit.analysis_fixtures import dossier
    from tests.unit.analysis_fixtures import seed_run as seed_analysis
    from vc_scout.llm.fake import FakeProvider

    store = RunStore("source-test", runs_root=tmp_path)
    seed_analysis(store, [dossier(company_id="co-00", claims=4)])
    provider = FakeProvider()
    monkeypatch.setattr(cli, "FakeProvider", lambda: provider)

    result = runner.invoke(
        app,
        [
            "analyze",
            "--run-id",
            "source-test",
            "--runs-root",
            str(tmp_path),
            "--provider",
            "fake",
            "--company-id",
            "not-a-candidate",
        ],
    )
    assert result.exit_code == 1
    assert "has no candidate 'not-a-candidate'" in result.output
    assert "co-00" in result.output
    assert provider.call_count == 0
    assert not store.analysis_report_path().exists()


def test_a_filtered_rerun_does_not_need_force_for_other_candidates(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Analysing a fresh candidate must not be blocked by other candidates' analyses."""
    from tests.unit.analysis_fixtures import dossier
    from tests.unit.analysis_fixtures import seed_run as seed_analysis
    from vc_scout.llm.fake import FakeProvider

    store = RunStore("source-test", runs_root=tmp_path)
    seed_analysis(
        store, [dossier(company_id="co-00", claims=4), dossier(company_id="co-01", claims=4)]
    )
    monkeypatch.setattr(cli, "FakeProvider", FakeProvider)
    base = [
        "analyze",
        "--run-id",
        "source-test",
        "--runs-root",
        str(tmp_path),
        "--provider",
        "fake",
    ]

    assert runner.invoke(app, [*base, "--company-id", "co-00"]).exit_code == 0
    # co-01 has no analysis yet, so no --force is required.
    assert runner.invoke(app, [*base, "--company-id", "co-01"]).exit_code == 0
    # co-00 does, so re-running it does.
    assert runner.invoke(app, [*base, "--company-id", "co-00"]).exit_code == 1
    assert runner.invoke(app, [*base, "--company-id", "co-00", "--force"]).exit_code == 0
