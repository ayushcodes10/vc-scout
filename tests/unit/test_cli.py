"""CLI surface.

The full command set is part of the delivered contract, so it is pinned by a test rather
than left to drift. Unimplemented commands must say so and exit non-zero - never exit 0
having done nothing.
"""

from __future__ import annotations

import re
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
    "recommend",
    "render",
    "build-ui",
    "build-site",
    "serve",
    "run",
    "demo",
]

runner = CliRunner()

_ANSI = re.compile(r"\x1b\[[0-9;]*m")


def help_text(*args: str) -> str:
    """Help output with styling removed.

    Typer renders help through rich, which styles *inside* a token - ``--query`` comes
    back as ``\x1b[1;36m-\x1b[0m\x1b[1;36m-query\x1b[0m``, so a plain substring check
    finds nothing. Whether that styling is emitted depends on the terminal the runner
    detects, which differs between a developer's shell and a CI runner: the assertions
    below passed locally and failed on CI for a reason that had nothing to do with the CLI.

    Stripping the escapes also restores the *negative* assertions. Under styling,
    "--provider is not in this help" was true of every help text, including one that
    listed it.
    """
    return _ANSI.sub("", runner.invoke(app, [*args, "--help"]).output)


def test_help_lists_every_required_command() -> None:
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    output = _ANSI.sub("", result.output)
    for command in REQUIRED_COMMANDS:
        assert command in output


@pytest.mark.parametrize("command", REQUIRED_COMMANDS)
def test_each_command_has_its_own_help(command: str) -> None:
    result = runner.invoke(app, [command, "--help"])
    assert result.exit_code == 0


@pytest.mark.parametrize(
    ("command", "args"),
    [
        ("serve", ["--run-id", "demo"]),
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


# -- recommend ---------------------------------------------------------------


def _seed_for_recommend(tmp_path: Path) -> RunStore:
    from tests.unit.memo_fixtures import bundles, mismatch_analysis, seed_rendered_run

    store = RunStore("source-test", runs_root=tmp_path)
    seeds = bundles(3)
    seed_rendered_run(store, [(bundle, mismatch_analysis(bundle)) for bundle in seeds])
    return store


def test_recommend_renders_memos_and_reports_the_shortlist(tmp_path: Path) -> None:
    _seed_for_recommend(tmp_path)
    result = runner.invoke(
        app, ["recommend", "--run-id", "source-test", "--runs-root", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    assert "Rendered 3 of 3 candidate memo(s)" in result.output
    assert "Recommendations: pass=3" in result.output
    assert "ranking.md" in result.output
    assert "recommendation-report.json" in result.output
    for company_id in ("co-00", "co-01", "co-02"):
        assert company_id in result.output
        assert (tmp_path / "source-test" / "memos" / f"{company_id}.md").is_file()


def test_recommend_needs_no_provider_and_no_api_key(tmp_path: Path) -> None:
    """The command declares no --provider and no --model, and reads no credential."""
    _seed_for_recommend(tmp_path)
    rendered = help_text("recommend")
    assert "--provider" not in rendered
    assert "--model" not in rendered
    assert "API_KEY" not in rendered

    result = runner.invoke(
        app, ["recommend", "--run-id", "source-test", "--runs-root", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output


def test_recommend_refuses_to_clobber_existing_output_without_force(tmp_path: Path) -> None:
    _seed_for_recommend(tmp_path)
    first = runner.invoke(
        app, ["recommend", "--run-id", "source-test", "--runs-root", str(tmp_path)]
    )
    assert first.exit_code == 0

    second = runner.invoke(
        app, ["recommend", "--run-id", "source-test", "--runs-root", str(tmp_path)]
    )
    assert second.exit_code == 1
    assert "already has rendered output" in second.output
    assert "--force" in second.output

    forced = runner.invoke(
        app,
        ["recommend", "--run-id", "source-test", "--runs-root", str(tmp_path), "--force"],
    )
    assert forced.exit_code == 0, forced.output


def test_recommend_requires_an_analysed_run(tmp_path: Path) -> None:
    result = runner.invoke(app, ["recommend", "--run-id", "missing", "--runs-root", str(tmp_path)])
    assert result.exit_code == 1
    assert "candidates.json" in result.output


def test_recommend_rejects_an_unusable_run_id(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["recommend", "--run-id", "../escape", "--runs-root", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "invalid run id" in result.output


def test_recommend_reports_an_unreachable_meeting_band(tmp_path: Path) -> None:
    from tests.unit.memo_fixtures import bundles, seed_rendered_run, thin_analysis

    store = RunStore("source-test", runs_root=tmp_path)
    seeds = bundles(2)
    seed_rendered_run(store, [(bundle, thin_analysis(bundle)) for bundle in seeds])
    result = runner.invoke(
        app, ["recommend", "--run-id", "source-test", "--runs-root", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    assert "the take-a-meeting band was unreachable for 2 of 2 candidate(s)" in result.output


def test_recommend_surfaces_a_candidate_that_could_not_be_rendered(tmp_path: Path) -> None:
    from tests.unit.memo_fixtures import bundles, mismatch_analysis, seed_rendered_run

    store = RunStore("source-test", runs_root=tmp_path)
    seeds = bundles(2)
    seed_rendered_run(store, [(seeds[0], mismatch_analysis(seeds[0])), (seeds[1], None)])
    result = runner.invoke(
        app, ["recommend", "--run-id", "source-test", "--runs-root", str(tmp_path)]
    )

    # One candidate failing is not a run failure: the exit code stays 0 and the gap is
    # reported rather than hidden.
    assert result.exit_code == 0, result.output
    assert "Rendered 1 of 2 candidate memo(s)" in result.output
    assert "co-01: no analysis was produced" in result.output


def test_render_still_works_as_a_deprecated_alias(tmp_path: Path) -> None:
    _seed_for_recommend(tmp_path)
    result = runner.invoke(app, ["render", "--run-id", "source-test", "--runs-root", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "this command is now `vc-scout recommend`" in result.output
    assert "Rendered 3 of 3 candidate memo(s)" in result.output


# -- build-ui ----------------------------------------------------------------


def _seed_for_ui(tmp_path: Path) -> RunStore:
    from tests.unit.memo_fixtures import bundles, mismatch_analysis, seed_rendered_run
    from vc_scout.stages.recommend import run_recommend

    store = RunStore("source-test", runs_root=tmp_path)
    seeds = bundles(3)
    seed_rendered_run(store, [(bundle, mismatch_analysis(bundle)) for bundle in seeds])
    run_recommend(store=store)
    return store


def test_build_ui_generates_the_site_and_prints_the_preview_command(tmp_path: Path) -> None:
    store = _seed_for_ui(tmp_path)
    result = runner.invoke(
        app, ["build-ui", "--run-id", "source-test", "--runs-root", str(tmp_path)]
    )

    assert result.exit_code == 0, result.output
    assert "Generated 4 page(s) for 3 candidate(s)" in result.output
    assert "python3 -m http.server 8000 --directory" in result.output
    assert (store.site_dir / "index.html").is_file()
    assert (store.site_dir / "companies" / "co-00.html").is_file()


def test_build_ui_needs_no_provider_and_no_api_key(tmp_path: Path) -> None:
    _seed_for_ui(tmp_path)
    rendered = help_text("build-ui")
    assert "--provider" not in rendered
    assert "--model" not in rendered
    assert "API_KEY" not in rendered

    result = runner.invoke(
        app, ["build-ui", "--run-id", "source-test", "--runs-root", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output


def test_build_ui_refuses_to_overwrite_an_existing_site_without_force(tmp_path: Path) -> None:
    _seed_for_ui(tmp_path)
    first = runner.invoke(
        app, ["build-ui", "--run-id", "source-test", "--runs-root", str(tmp_path)]
    )
    assert first.exit_code == 0

    second = runner.invoke(
        app, ["build-ui", "--run-id", "source-test", "--runs-root", str(tmp_path)]
    )
    assert second.exit_code == 1
    assert "already has a generated site" in second.output
    assert "--force" in second.output

    forced = runner.invoke(
        app,
        ["build-ui", "--run-id", "source-test", "--runs-root", str(tmp_path), "--force"],
    )
    assert forced.exit_code == 0, forced.output


def test_build_ui_requires_the_markdown_stage_first(tmp_path: Path) -> None:
    result = runner.invoke(app, ["build-ui", "--run-id", "missing", "--runs-root", str(tmp_path)])
    assert result.exit_code == 1
    assert "candidates.json" in result.output


def test_build_ui_rejects_an_unusable_run_id(tmp_path: Path) -> None:
    result = runner.invoke(app, ["build-ui", "--run-id", "../escape", "--runs-root", str(tmp_path)])
    assert result.exit_code == 1
    assert "invalid run id" in result.output


def test_build_site_still_works_as_a_deprecated_alias(tmp_path: Path) -> None:
    _seed_for_ui(tmp_path)
    result = runner.invoke(
        app, ["build-site", "--run-id", "source-test", "--runs-root", str(tmp_path)]
    )
    assert result.exit_code == 0, result.output
    assert "this command is now `vc-scout build-ui`" in result.output
    assert "Generated 4 page(s)" in result.output


# -- run, demo and export-demo -----------------------------------------------


def test_run_help_puts_the_happy_path_first() -> None:
    output = help_text("run")
    for option in ("--query", "--limit", "--run-id", "--provider", "--model", "--effort"):
        assert option in output
    for option in ("--max-extra-pages", "--force-stage", "--stop-after"):
        assert option in output
    assert "10<=x<=20" in output


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--force-stage", "nonsense"], "--force-stage must be one of"),
        (["--stop-after", "nonsense"], "--stop-after must be one of"),
        (["--provider", "openai"], "unknown provider"),
        (["--query", "   "], "--query must not be empty"),
    ],
)
def test_run_validates_every_option_before_touching_the_filesystem(
    tmp_path: Path, args: list[str], message: str
) -> None:
    base = ["run", "--query", "q", "--run-id", "demo-run", "--runs-root", str(tmp_path)]
    # The bad value replaces the good one where they collide.
    invocation = base + args
    result = runner.invoke(app, invocation)

    assert result.exit_code == 1
    assert message in result.output
    assert not (tmp_path / "demo-run").exists()


def test_run_rejects_a_limit_outside_the_band(tmp_path: Path) -> None:
    for limit in ("9", "21"):
        result = runner.invoke(
            app,
            [
                "run",
                "--query",
                "q",
                "--run-id",
                "r",
                "--runs-root",
                str(tmp_path),
                "--limit",
                limit,
            ],
        )
        assert result.exit_code != 0
        assert not (tmp_path / "r").exists()


def test_run_refuses_the_live_provider_without_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    result = runner.invoke(
        app,
        [
            "run",
            "--query",
            "q",
            "--run-id",
            "live-run",
            "--runs-root",
            str(tmp_path),
            "--provider",
            "anthropic",
        ],
    )
    assert result.exit_code == 1
    assert "ANTHROPIC_API_KEY is not set" in result.output
    assert not (tmp_path / "live-run" / "candidates.json").exists()


def test_demo_runs_the_whole_pipeline_without_a_credential(tmp_path: Path) -> None:
    result = runner.invoke(app, ["demo", "--run-id", "offline-demo", "--runs-root", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert "no credential required" in result.output
    for stage in ("source", "enrich", "evidence", "analysis", "recommend", "ui"):
        assert stage in result.output
    assert "Preview the site with:" in result.output
    store = RunStore("offline-demo", runs_root=tmp_path)
    assert store.run_report_path().is_file()
    assert (store.site_dir / "index.html").is_file()
    assert list(store.resolve("memos").glob("*.md"))


def test_demo_repeated_resumes_every_stage(tmp_path: Path) -> None:
    runner.invoke(app, ["demo", "--run-id", "offline-demo", "--runs-root", str(tmp_path)])
    result = runner.invoke(app, ["demo", "--run-id", "offline-demo", "--runs-root", str(tmp_path)])

    assert result.exit_code == 0, result.output
    assert result.output.count("resumed") >= 6


def test_export_demo_writes_the_reviewer_directory(tmp_path: Path) -> None:
    runner.invoke(app, ["demo", "--run-id", "offline-demo", "--runs-root", str(tmp_path)])
    destination = tmp_path / "demo-export"
    result = runner.invoke(
        app,
        [
            "export-demo",
            "--run-id",
            "offline-demo",
            "--runs-root",
            str(tmp_path),
            "--destination",
            str(destination),
        ],
    )

    assert result.exit_code == 0, result.output
    assert "memo(s)" in result.output
    assert "AI trace:" in result.output
    assert (destination / "README.md").is_file()
    assert (destination / "site" / "index.html").is_file()


def test_export_demo_refuses_a_run_that_was_never_rendered(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["export-demo", "--run-id", "nothing", "--runs-root", str(tmp_path)]
    )
    assert result.exit_code == 1
    assert "recommendation-report.json" in result.output
