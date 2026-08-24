"""Targeted recovery of the candidates an analysis run failed on.

The live case this exists for: thirteen of fifteen analyses good, two rejected on shape.
Re-running all fifteen would spend twenty-six requests to repair two and would throw away
thirteen analyses a partner could already read.

So the properties worth testing are mostly about restraint - what recovery does *not* touch
- and about the merge being a merge rather than a replacement.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.integration.test_pipeline import execute
from vc_scout.llm.analysis_schema import ANALYSIS_TOOL_NAME
from vc_scout.llm.fake import FakeProvider, derive_response
from vc_scout.llm.provider import LlmRequest, ModelConfig
from vc_scout.models.enums import PipelineStage, PipelineStageStatus
from vc_scout.models.report import StageRun
from vc_scout.pipeline import apply_recovery
from vc_scout.stages.recover import RecoveryError, recover_analyses
from vc_scout.store import RunStore

CONFIG = ModelConfig(model="fake-model-1", max_tokens=4096, effort="low")

#: The live failure mode: a response missing one of the four required sections.
FAILING = ("deskloop", "rotamesh")


def company_of(request: LlmRequest) -> str:
    for line in request.user_payload.splitlines():
        if line.startswith("company_id: "):
            return line.removeprefix("company_id: ").strip()
    return ""


def dropping(fail_for: set[str]) -> FakeProvider:
    """A provider that omits the market section for the named companies."""

    def handler(request: LlmRequest) -> Any:
        payload = derive_response(request)
        if request.schema_name == ANALYSIS_TOOL_NAME and company_of(request) in fail_for:
            payload = dict(payload)
            payload["sections"] = [
                section for section in payload["sections"] if section["kind"] != "market"
            ]
        return payload

    return FakeProvider(handler=handler)


@pytest.fixture
def broken(tmp_path: Path) -> RunStore:
    """A completed run in which exactly two candidates failed analysis."""
    store = RunStore("ai-smb-ops-demo", runs_root=tmp_path)
    execute(store, provider=dropping(set(FAILING)))
    report = store.read_analysis_report()
    assert sorted(row.company_id for row in report.candidates if not row.succeeded) == sorted(
        FAILING
    )
    return store


def recover(store: RunStore, provider: Any = None, **kwargs: Any) -> Any:
    return recover_analyses(
        store=store, provider=provider or FakeProvider(), config=CONFIG, **kwargs
    )


# -- selection ---------------------------------------------------------------


def test_only_the_failed_candidates_are_sent_to_the_provider(broken: RunStore) -> None:
    provider = FakeProvider()
    outcome = recover(broken, provider)

    assert sorted(outcome.attempted) == sorted(FAILING)
    assert {company_of(request) for request in provider.requests} == set(FAILING)
    assert all(request.schema_name == ANALYSIS_TOOL_NAME for request in provider.requests)
    # Two candidates, one attempt each: the successes cost nothing.
    assert provider.call_count == len(FAILING)


def test_a_run_with_no_failures_makes_no_call(tmp_path: Path) -> None:
    store = RunStore("clean", runs_root=tmp_path)
    execute(store)
    provider = FakeProvider()

    outcome = recover(store, provider)

    assert provider.call_count == 0
    assert outcome.attempted == []
    assert outcome.recovered == []
    assert outcome.report.counts["failed"] == 0


def test_a_filtered_report_is_refused(broken: RunStore) -> None:
    report = broken.read_analysis_report()
    broken.write_analysis_report(report.model_copy(update={"filtered_to": "deskloop"}))
    provider = FakeProvider()

    with pytest.raises(RecoveryError, match="filtered to"):
        recover(broken, provider)
    assert provider.call_count == 0


def test_only_recorded_failures_may_be_named(broken: RunStore) -> None:
    report = broken.read_analysis_report()
    succeeded = next(row.company_id for row in report.candidates if row.succeeded)
    provider = FakeProvider()

    with pytest.raises(RecoveryError, match="recorded as succeeded"):
        recover(broken, provider, only=[succeeded])
    with pytest.raises(RecoveryError, match="no candidate"):
        recover(broken, provider, only=["not-in-this-run"])
    assert provider.call_count == 0


def test_naming_one_failure_recovers_only_that_one(broken: RunStore) -> None:
    provider = FakeProvider()
    outcome = recover(broken, provider, only=[FAILING[0]])

    assert outcome.attempted == [FAILING[0]]
    assert {company_of(request) for request in provider.requests} == {FAILING[0]}
    assert FAILING[1] in outcome.still_failed


# -- what recovery leaves alone ----------------------------------------------


def test_every_previously_successful_analysis_is_byte_identical(broken: RunStore) -> None:
    before = {
        company_id: broken.analysis_path(company_id).read_bytes()
        for company_id in broken.analysis_company_ids()
    }
    recover(broken)

    for company_id, blob in before.items():
        assert broken.analysis_path(company_id).read_bytes() == blob


def test_recovery_touches_no_upstream_artifact(broken: RunStore) -> None:
    before = {
        "candidates": broken.candidates_path().read_bytes(),
        "source": broken.source_report_path().read_bytes(),
        "enrichment": broken.enrichment_report_path().read_bytes(),
        "evidence": broken.evidence_report_path().read_bytes(),
        **{
            f"dossier:{cid}": broken.evidence_path(cid).read_bytes()
            for cid in broken.evidence_company_ids()
        },
    }
    recover(broken)

    assert broken.candidates_path().read_bytes() == before["candidates"]
    assert broken.source_report_path().read_bytes() == before["source"]
    assert broken.enrichment_report_path().read_bytes() == before["enrichment"]
    assert broken.evidence_report_path().read_bytes() == before["evidence"]
    for company_id in broken.evidence_company_ids():
        assert broken.evidence_path(company_id).read_bytes() == before[f"dossier:{company_id}"]


def test_only_the_recovered_candidates_attempt_files_are_replaced(broken: RunStore) -> None:
    responses = broken.resolve("llm", "analysis-responses")
    untouched = {
        path.name: path.read_bytes()
        for path in responses.glob("*.json")
        if not path.name.startswith(FAILING)
    }
    recover(broken)

    for name, blob in untouched.items():
        assert (responses / name).read_bytes() == blob
    # The failed candidate's two rejected attempts are gone; its one good attempt remains.
    remaining = sorted(p.name for p in responses.glob(f"{FAILING[0]}-attempt*.json"))
    assert remaining == [f"{FAILING[0]}-attempt1.json"]


# -- the merge ---------------------------------------------------------------


def test_the_merged_report_stays_full_and_keeps_its_order(broken: RunStore) -> None:
    before = [row.company_id for row in broken.read_analysis_report().candidates]
    outcome = recover(broken)

    assert [row.company_id for row in outcome.report.candidates] == before
    assert outcome.report.filtered_to is None
    assert len(outcome.report.candidates) == len(broken.read_candidates().candidates)


def test_every_aggregate_is_recomputed_from_the_merged_outcomes(broken: RunStore) -> None:
    outcome = recover(broken)
    report = outcome.report
    rows = report.candidates

    assert report.counts["candidates"] == len(rows)
    assert report.counts["succeeded"] == sum(1 for row in rows if row.succeeded)
    assert report.counts["failed"] == sum(1 for row in rows if not row.succeeded)
    assert report.counts["attempts"] == sum(len(row.attempts) for row in rows)
    assert report.counts["retried"] == sum(1 for row in rows if len(row.attempts) > 1)
    assert report.counts["input_tokens"] == sum(
        a.input_tokens for row in rows for a in row.attempts
    )
    assert report.counts["output_tokens"] == sum(
        a.output_tokens for row in rows for a in row.attempts
    )
    assert report.counts.get("model_policy_disagreements", 0) == sum(
        1 for row in rows if row.succeeded and row.model_disagreed
    )
    assert sum(report.recommendations.values()) == report.counts["succeeded"]
    for decision, total in report.recommendations.items():
        assert total == sum(
            1 for row in rows if row.succeeded and row.decision and row.decision.value == decision
        )
    assert sum(report.failures_by_category.values()) == report.counts["failed"]


def test_the_attempt_history_keeps_the_original_round(broken: RunStore) -> None:
    before = {
        row.company_id: len(row.attempts)
        for row in broken.read_analysis_report().candidates
        if not row.succeeded
    }
    outcome = recover(broken)

    for company_id, original in before.items():
        row = next(r for r in outcome.report.candidates if r.company_id == company_id)
        assert len(row.attempts) > original
        assert [a.recovery_round for a in row.attempts[:original]] == [0] * original
        assert all(a.recovery_round == 1 for a in row.attempts[original:])
    assert "Recovery round 1" in " ".join(outcome.report.notes)


def test_the_analysis_fingerprint_is_updated_for_the_current_evidence(
    broken: RunStore,
) -> None:
    from vc_scout.fingerprint import evidence_fingerprint

    outcome = recover(broken)
    expected = evidence_fingerprint(
        [broken.read_evidence(cid) for cid in broken.evidence_company_ids()]
    )
    assert outcome.report.upstream_fingerprint == expected


def test_one_candidate_can_recover_while_another_stays_failed(broken: RunStore) -> None:
    outcome = recover(broken, dropping({FAILING[1]}))

    assert outcome.recovered == [FAILING[0]]
    assert outcome.still_failed == [FAILING[1]]
    assert outcome.report.counts["failed"] == 1
    assert broken.analysis_path(FAILING[0]).is_file()
    assert not broken.analysis_path(FAILING[1]).exists()


def test_a_report_never_claims_a_success_it_cannot_show(broken: RunStore) -> None:
    outcome = recover(broken)
    assert outcome.verified is True

    # Remove a file the report says exists; verification must notice.
    from vc_scout.stages.recover import _verify

    broken.analysis_path(outcome.recovered[0]).unlink()
    assert _verify(broken, outcome.report) is False


# -- downstream --------------------------------------------------------------


def stage_record(store: RunStore, stage: PipelineStage) -> StageRun:
    return next(record for record in store.read_run_report().stages if record.stage is stage)


def rebuild(store: RunStore, outcome: Any) -> Any:
    return apply_recovery(
        store=store,
        analysis_stage=StageRun(
            stage=PipelineStage.ANALYSIS,
            status=(
                PipelineStageStatus.PARTIAL
                if outcome.still_failed
                else PipelineStageStatus.COMPLETED
            ),
            decision=f"recovery round {outcome.recovery_round}",
            candidates_in=outcome.report.counts["candidates"],
            candidates_out=outcome.report.counts["succeeded"],
            failures=outcome.report.counts["failed"],
            upstream_fingerprint=outcome.report.upstream_fingerprint,
        ),
        rebuild=outcome.changed,
    )


def test_the_memos_and_the_site_are_rebuilt_with_the_recovered_candidates(
    broken: RunStore,
) -> None:
    before = broken.read_recommendation_report().memos_written
    outcome = recover(broken)
    result = rebuild(broken, outcome)

    assert result.rebuilt is True
    assert result.memos == before + len(outcome.recovered)
    assert result.pages == result.memos + 1  # every company page, plus the index
    for company_id in outcome.recovered:
        assert broken.memo_path(company_id).is_file()
        assert (broken.site_dir / "companies" / f"{company_id}.html").is_file()
    assert broken.read_recommendation_report().memos_written == result.memos


def test_a_still_failed_candidate_keeps_no_memo_or_page(broken: RunStore) -> None:
    outcome = recover(broken, dropping({FAILING[1]}))
    rebuild(broken, outcome)

    assert not broken.memo_path(FAILING[1]).exists()
    assert not (broken.site_dir / "companies" / f"{FAILING[1]}.html").exists()
    assert FAILING[1] in " ".join(
        failure.company_id for failure in broken.read_recommendation_report().failures
    )


def test_the_run_report_reconciles_without_disturbing_upstream_stages(
    broken: RunStore,
) -> None:
    before = {
        stage: stage_record(broken, stage)
        for stage in (PipelineStage.SOURCE, PipelineStage.ENRICH, PipelineStage.EVIDENCE)
    }
    outcome = recover(broken)
    rebuild(broken, outcome)
    report = broken.read_run_report()

    for stage, record in before.items():
        assert stage_record(broken, stage) == record

    analysis = stage_record(broken, PipelineStage.ANALYSIS)
    assert analysis.candidates_out == outcome.report.counts["succeeded"]
    assert analysis.failures == outcome.report.counts["failed"]
    assert "recovery round 1" in (analysis.decision or "")

    recommendation = broken.read_recommendation_report()
    assert report.recommendations == recommendation.recommendations
    assert report.token_usage["analysis_input_tokens"] == outcome.report.counts["input_tokens"]
    assert stage_record(broken, PipelineStage.RECOMMEND).candidates_out == (
        recommendation.memos_written
    )


def test_nothing_is_rebuilt_when_nothing_recovered(broken: RunStore) -> None:
    before = broken.read_recommendation_report().memos_written
    outcome = recover(broken, dropping(set(FAILING)))

    assert outcome.recovered == []
    result = rebuild(broken, outcome)
    assert result.rebuilt is False
    assert broken.read_recommendation_report().memos_written == before


def test_recovery_makes_the_run_resumable_again(broken: RunStore) -> None:
    """After a repair, `run` must see current artifacts rather than rerun everything."""
    from tests.integration.test_pipeline import resumed

    outcome = recover(broken)
    rebuild(broken, outcome)
    result = execute(broken, offline=True)

    assert all(resumed(result).values())


# -- prompt and schema -------------------------------------------------------


def test_the_prompt_states_every_required_shape() -> None:
    from vc_scout.prompts import prompt_text
    from vc_scout.stages.analysis import ANALYSIS_PROMPT_VERSION

    assert ANALYSIS_PROMPT_VERSION == "analysis_v2.1"
    text = prompt_text(ANALYSIS_PROMPT_VERSION)
    head = text[: text.index("# What you are scoring")]

    assert "Required shape - read this first" in head
    for kind in ("team", "product", "market", "thesis"):
        assert f"`{kind}`" in head
    for dimension in (
        "pain_roi",
        "wedge",
        "distribution",
        "defensibility",
        "team",
        "traction",
        "market_timing",
    ):
        assert dimension in head
    assert "two or three" in head
    assert "conflict" in head and "caveats" in head
    assert "including when the evidence is thin" in head


def test_a_retry_repeats_the_required_shape(broken: RunStore) -> None:
    from vc_scout.stages.analysis import render_dossier_payload

    candidate = broken.read_candidates().candidates[0]
    dossier = broken.read_evidence(candidate.company_id)
    payload = render_dossier_payload(
        candidate, dossier, validation_errors=["sections: missing kind 'market'"]
    )

    assert "Correction required" in payload
    assert "exactly one `team`, one `product`, one `market` and one `thesis` section" in payload
    assert "all seven score_components" in payload
    assert "exactly two or three recommendation_changers" in payload
    assert "no component marked supported where the dossier records a conflict" in payload
    assert "never changes this shape" in payload


def test_the_compact_schema_is_still_inside_its_budget() -> None:
    import json

    from vc_scout.llm.analysis_schema import ANALYSIS_SCHEMA

    assert len(json.dumps(ANALYSIS_SCHEMA, separators=(",", ":"))) <= 2400


# -- the command -------------------------------------------------------------


def test_the_command_recovers_and_rebuilds(broken: RunStore, tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from vc_scout.cli import app

    result = CliRunner().invoke(
        app,
        [
            "recover-analysis",
            "--run-id",
            "ai-smb-ops-demo",
            "--runs-root",
            str(tmp_path),
            "--provider",
            "fake",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Recovering 2 failed candidate(s)" in result.output
    assert "at most 4 request(s)" in result.output
    for company_id in FAILING:
        assert f"recovered  {company_id}" in result.output
    assert "Rebuilt" in result.output
    assert broken.read_analysis_report().counts["failed"] == 0


def test_the_command_makes_no_call_and_says_so_when_nothing_failed(tmp_path: Path) -> None:
    from typer.testing import CliRunner

    from vc_scout.cli import app

    store = RunStore("clean", runs_root=tmp_path)
    execute(store)
    result = CliRunner().invoke(
        app,
        [
            "recover-analysis",
            "--run-id",
            "clean",
            "--runs-root",
            str(tmp_path),
            "--provider",
            "fake",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Nothing to recover" in result.output
    assert "No provider call was made" in result.output


def test_the_command_refuses_a_filtered_report_before_choosing_a_provider(
    broken: RunStore, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from typer.testing import CliRunner

    from vc_scout.cli import app

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    report = broken.read_analysis_report()
    broken.write_analysis_report(report.model_copy(update={"filtered_to": "deskloop"}))

    result = CliRunner().invoke(
        app,
        [
            "recover-analysis",
            "--run-id",
            "ai-smb-ops-demo",
            "--runs-root",
            str(tmp_path),
            "--provider",
            "anthropic",
        ],
    )
    # The refusal comes from the report, not from the missing credential: planning happens
    # before a provider is built.
    assert result.exit_code == 1
    assert "filtered to" in result.output
    assert "ANTHROPIC_API_KEY" not in result.output
