"""Reported headroom: the highest total an analysis could have reached.

The live run scored every candidate below the take-a-meeting band and it was not obvious
from the report *why*: no component was ever graded ``supported``, so the rubric ceilings
alone held every achievable total under 80. That is a property of the evidence, not of the
model's judgement, and a reader deserves to see it rather than reconstruct it.

These fields are report metadata. They are computed from statuses and rubric ceilings, and
they must never feed the score, the confidence or the recommendation.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from tests.unit.analysis_fixtures import NOW, analysis_payload, dossier, seed_run
from vc_scout.cli import _report_analysis, app
from vc_scout.llm.fake import FakeProvider, derive_response
from vc_scout.llm.provider import LlmRequest, ModelConfig
from vc_scout.models.analysis import ceiling_for
from vc_scout.models.enums import AssessmentStatus, RubricDimension
from vc_scout.policy import TAKE_A_MEETING_AT
from vc_scout.rubric import MAX_TOTAL_SCORE, RUBRIC
from vc_scout.stages.analysis import run_analysis
from vc_scout.store import RunStore

CONFIG = ModelConfig(model="fake-model-1", max_tokens=4096, effort="low")
runner = CliRunner()

# The ceilings the rubric arithmetic implies, spelled out so a weight change breaks a test
# rather than silently moving the band out of reach.
ALL_SUPPORTED = 100
ALL_PARTIAL = 68
ALL_UNASSESSABLE = 48


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore("source-test", runs_root=tmp_path)


def test_the_spelled_out_ceilings_match_the_live_rubric() -> None:
    def total(status: AssessmentStatus) -> int:
        return sum(ceiling_for(spec.key, status) for spec in RUBRIC)

    assert total(AssessmentStatus.SUPPORTED) == ALL_SUPPORTED == MAX_TOTAL_SCORE
    assert total(AssessmentStatus.PARTIALLY_SUPPORTED) == ALL_PARTIAL
    assert total(AssessmentStatus.NOT_ASSESSABLE) == ALL_UNASSESSABLE
    # The finding the live run surfaced: without a single supported component, 80 is
    # arithmetically unreachable.
    assert ALL_PARTIAL < TAKE_A_MEETING_AT


def run_with_status(store: RunStore, status: str) -> object:
    bundle = dossier(claims=6)
    seed_run(store, [bundle])
    payload = analysis_payload(bundle, status=status)
    return run_analysis(store=store, provider=FakeProvider([payload]), config=CONFIG, now=NOW)


@pytest.mark.parametrize(
    ("status", "expected", "reachable"),
    [
        ("supported", ALL_SUPPORTED, True),
        ("partially_supported", ALL_PARTIAL, False),
        ("not_assessable", ALL_UNASSESSABLE, False),
    ],
)
def test_headroom_follows_the_assessment_statuses(
    store: RunStore, status: str, expected: int, reachable: bool
) -> None:
    outcome = run_with_status(store, status)
    row = outcome.report.candidates[0]  # type: ignore[attr-defined]
    assert row.maximum_achievable_score == expected
    assert row.meeting_reachable_by_statuses is reachable


def test_headroom_reconciles_with_the_stored_analysis_component_by_component(
    store: RunStore,
) -> None:
    bundle = dossier(claims=6)
    seed_run(store, [bundle])
    mixed = analysis_payload(
        bundle,
        status="partially_supported",
        scores={
            RubricDimension.PAIN_ROI: 14,
            RubricDimension.WEDGE: 10,
            RubricDimension.DISTRIBUTION: 10,
            RubricDimension.DEFENSIBILITY: 10,
            RubricDimension.TEAM: 10,
            RubricDimension.TRACTION: 7,
            RubricDimension.MARKET_TIMING: 7,
        },
    )
    outcome = run_analysis(store=store, provider=FakeProvider([mixed]), config=CONFIG, now=NOW)

    stored, _ = store.read_analysis(bundle.company_id)
    row = outcome.report.candidates[0]
    assert row.maximum_achievable_score == sum(
        ceiling_for(c.component, c.assessment_status) for c in stored.score_components
    )
    # Every component sits at its own ceiling here, so the two totals coincide - the one
    # case where headroom and the actual score are allowed to be equal.
    assert row.maximum_achievable_score == stored.total_score == ALL_PARTIAL


def test_headroom_is_never_below_the_score_it_reports(store: RunStore) -> None:
    bundles = [dossier(company_id=f"co-{i:02d}", claims=6) for i in range(4)]
    seed_run(store, bundles)
    outcome = run_analysis(store=store, provider=FakeProvider(), config=CONFIG, now=NOW)
    for row in outcome.report.candidates:
        assert row.maximum_achievable_score is not None
        assert row.total_score is not None
        assert row.total_score <= row.maximum_achievable_score <= MAX_TOTAL_SCORE
        assert row.meeting_reachable_by_statuses == (
            row.maximum_achievable_score >= TAKE_A_MEETING_AT
        )


def test_a_failed_candidate_reports_no_headroom_at_all(store: RunStore) -> None:
    """Absent an analysis there are no statuses, so the honest answer is `null`, not 0."""
    bundle = dossier(claims=6)
    seed_run(store, [bundle])
    broken = analysis_payload(bundle, changers=0)
    outcome = run_analysis(
        store=store, provider=FakeProvider([broken, dict(broken)]), config=CONFIG, now=NOW
    )
    row = outcome.report.candidates[0]
    assert row.succeeded is False
    assert row.maximum_achievable_score is None
    assert row.meeting_reachable_by_statuses is None


def test_headroom_changes_no_score_band_or_guardrail(store: RunStore, tmp_path: Path) -> None:
    """The fields are metadata. Recompute the run without reading them and nothing moves."""
    bundles = [dossier(company_id=f"co-{i:02d}", claims=6) for i in range(3)]
    seed_run(store, bundles)
    outcome = run_analysis(store=store, provider=FakeProvider(), config=CONFIG, now=NOW)
    for row in outcome.report.candidates:
        stored, decision = store.read_analysis(row.company_id)
        assert decision is not None
        assert row.total_score == stored.total_score
        assert row.decision is decision.decision
        assert row.confidence_level is stored.research_confidence.level
        # Nothing about the ceiling arithmetic leaks into the policy's own record.
        assert "maximum_achievable" not in decision.model_dump_json()
        assert "reachable" not in decision.model_dump_json()


def test_the_report_persists_both_fields(store: RunStore) -> None:
    bundle = dossier(claims=6)
    seed_run(store, [bundle])
    run_analysis(
        store=store,
        provider=FakeProvider([analysis_payload(bundle, status="partially_supported")]),
        config=CONFIG,
        now=NOW,
    )
    written = store.analysis_report_path().read_text()
    assert '"maximum_achievable_score": 68' in written
    assert '"meeting_reachable_by_statuses": false' in written


# -- CLI surface -------------------------------------------------------------


def test_the_cli_shows_headroom_per_row_and_stays_quiet_when_the_band_is_reachable(
    store: RunStore, tmp_path: Path
) -> None:
    seed_run(store, [dossier(claims=6)])
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
            "--force",
        ],
    )
    assert result.exit_code == 0, result.output
    report = store.read_analysis_report()
    row = report.candidates[0]
    assert row.meeting_reachable_by_statuses is True
    assert re.search(rf"max=\s*{row.maximum_achievable_score}(?! -)", result.output)
    assert "unreachable" not in result.output


def test_the_cli_marks_a_row_whose_band_is_out_of_reach(
    store: RunStore, capsys: pytest.CaptureFixture[str]
) -> None:
    """Rendered from a real outcome, because the fake provider always leaves 80 reachable."""
    bundles = [dossier(company_id=f"co-{i:02d}", claims=6) for i in range(2)]
    seed_run(store, bundles)

    def handler(request: LlmRequest) -> object:
        if "co-00" in request.user_payload:
            return analysis_payload(bundles[0], status="partially_supported")
        return derive_response(request)

    outcome = run_analysis(
        store=store, provider=FakeProvider(handler=handler), config=CONFIG, now=NOW
    )
    capsys.readouterr()
    _report_analysis(outcome)
    output = capsys.readouterr().out

    assert re.search(r"co-00 .*max=\s*68 -", output)
    assert re.search(r"co-01 .*max=\s*\d+ ", output)  # reachable rows carry no dash
    assert "the take-a-meeting band was unreachable for 1 of 2 analysed candidate(s)" in output
