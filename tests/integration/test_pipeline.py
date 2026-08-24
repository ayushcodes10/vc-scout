"""The one-command pipeline, end to end against committed fixtures.

No socket is opened: `tests/conftest.py` blocks that outright, and the network clients here
are the production ones over an `httpx.MockTransport`. The fake provider makes the *model*
offline; it does nothing about HTTP, so sourcing and enrichment are made offline separately
and deliberately.

What these tests are really about is the two things orchestration adds over the stages it
calls: which failures stop a run, and when an existing artifact may be trusted.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from vc_scout.demo_fixtures import DEMO_QUERY, demo_client, demo_fetcher
from vc_scout.llm.fake import FakeProvider
from vc_scout.llm.provider import LlmError, LlmRequest, LlmResult
from vc_scout.models.enums import LlmErrorCategory, PipelineStage, PipelineStageStatus
from vc_scout.net.hn import HnError
from vc_scout.pipeline import PipelineAbortedError, Plan, run_pipeline
from vc_scout.store import RunStore

STAGES = list(PipelineStage)


class CountingProvider(FakeProvider):
    """The deterministic provider, plus a tally of what it was asked."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.calls: list[str] = []

    def complete_json(self, request: LlmRequest) -> LlmResult:
        self.calls.append(request.schema_name)
        return super().complete_json(request)


class AbortingProvider:
    """A provider whose very first call is a run-level failure."""

    name = "aborting"

    def __init__(self) -> None:
        self.calls = 0

    def complete_json(self, request: LlmRequest) -> LlmResult:  # noqa: ARG002
        self.calls += 1
        raise LlmError(
            LlmErrorCategory.PROVIDER_HTTP_ERROR,
            "HTTP 401: invalid api key",
            status=401,
            run_level=True,
        )


def no_network() -> Any:
    raise AssertionError("a network client was constructed when the stage should have resumed")


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore("offline-demo", runs_root=tmp_path)


def plan(**overrides: Any) -> Plan:
    base: dict[str, Any] = {
        "query": DEMO_QUERY,
        "limit": 10,
        "provider_name": "fake",
        "model": "fake-model-1",
        "effort": "low",
    }
    base.update(overrides)
    return Plan(**base)


def execute(store: RunStore, *, provider: Any = None, offline: bool = False, **overrides: Any):  # type: ignore[no-untyped-def]
    return run_pipeline(
        store=store,
        plan=plan(**overrides),
        provider=provider or FakeProvider(),
        client_factory=no_network if offline else demo_client,
        fetcher_factory=no_network if offline else demo_fetcher,
    )


def statuses(result: Any) -> dict[str, PipelineStageStatus]:
    return {record.stage.value: record.status for record in result.report.stages}


def resumed(result: Any) -> dict[str, bool]:
    return {record.stage.value: record.resumed for record in result.report.stages}


# -- the complete pipeline ---------------------------------------------------


def test_a_fixture_backed_run_produces_every_artifact(store: RunStore) -> None:
    result = execute(store)

    assert [record.stage for record in result.report.stages] == STAGES
    assert all(status is not PipelineStageStatus.FAILED for status in statuses(result).values())

    candidates = store.read_candidates().candidates
    assert len(candidates) >= 5
    for candidate in candidates:
        assert store.extracted_path(candidate.company_id).is_file()
        assert store.evidence_path(candidate.company_id).is_file()
        assert store.analysis_path(candidate.company_id).is_file()
        assert store.memo_path(candidate.company_id).is_file()
        assert (store.site_dir / "companies" / f"{candidate.company_id}.html").is_file()
    assert store.ranking_path().is_file()
    assert (store.site_dir / "index.html").is_file()
    assert store.run_report_path().is_file()


def test_the_run_report_reconciles_with_the_stage_reports(store: RunStore) -> None:
    result = execute(store)
    report = result.report

    assert report.run_id == "offline-demo"
    assert report.query == DEMO_QUERY
    assert report.requested_limit == 10
    assert report.provider == "fake"
    assert report.completed_at is not None

    kept = len(store.read_candidates().candidates)
    assert report.candidate_flow["source_out"] == kept
    assert report.candidate_flow["enrich_in"] == kept
    assert report.candidate_flow["evidence_in"] == kept

    recommendation = store.read_recommendation_report()
    assert report.recommendations == recommendation.recommendations
    assert sum(report.recommendations.values()) == recommendation.memos_written

    evidence = store.read_evidence_report()
    analysis = store.read_analysis_report()
    assert report.token_usage["evidence_input_tokens"] == evidence.counts["input_tokens"]
    assert report.token_usage["analysis_output_tokens"] == analysis.counts["output_tokens"]

    for key in (
        "source_formula",
        "evidence_prompt",
        "analysis_prompt",
        "thesis",
        "rubric",
        "policy",
        "memo_template",
        "ui_template",
    ):
        assert report.versions[key]
    assert set(report.resumability) == {stage.value for stage in STAGES}


def test_stages_run_in_order_and_each_records_a_decision(store: RunStore) -> None:
    order: list[PipelineStage] = []
    run_pipeline(
        store=store,
        plan=plan(),
        provider=FakeProvider(),
        client_factory=demo_client,
        fetcher_factory=demo_fetcher,
        on_stage=lambda record: order.append(record.stage),
    )
    assert order == STAGES
    for record in store.read_run_report().stages:
        assert record.decision


# -- resume ------------------------------------------------------------------


def test_a_completed_run_repeated_makes_no_client_or_provider_call(store: RunStore) -> None:
    execute(store)
    provider = CountingProvider()
    result = execute(store, provider=provider, offline=True)

    assert provider.calls == []
    assert all(resumed(result).values())
    assert all(status is PipelineStageStatus.COMPLETED for status in statuses(result).values())
    for line in result.report.resumability.values():
        assert line.startswith("resumed:")


def test_forcing_analysis_reruns_only_analysis_and_below(store: RunStore) -> None:
    execute(store)
    provider = CountingProvider()
    result = execute(
        store,
        provider=provider,
        offline=True,
        forced=frozenset({PipelineStage.ANALYSIS}),
    )

    was_resumed = resumed(result)
    assert was_resumed == {
        "source": True,
        "enrich": True,
        "evidence": True,
        "analysis": False,
        "recommend": False,
        "ui": False,
    }
    # Only the analysis tool was called: evidence was not re-extracted.
    from vc_scout.llm.analysis_schema import ANALYSIS_TOOL_NAME

    assert set(provider.calls) == {ANALYSIS_TOOL_NAME}


def test_forcing_source_invalidates_every_downstream_stage(store: RunStore) -> None:
    execute(store)
    result = execute(store, forced=frozenset({PipelineStage.SOURCE}))
    assert not any(resumed(result).values())


def test_forcing_a_late_stage_leaves_upstream_artifacts_untouched(store: RunStore) -> None:
    execute(store)
    company = store.read_candidates().candidates[0].company_id
    upstream = {
        "candidates": store.candidates_path().read_bytes(),
        "extracted": store.extracted_path(company).read_bytes(),
        "evidence": store.evidence_path(company).read_bytes(),
    }

    execute(store, offline=True, forced=frozenset({PipelineStage.RECOMMEND}))

    assert store.candidates_path().read_bytes() == upstream["candidates"]
    assert store.extracted_path(company).read_bytes() == upstream["extracted"]
    assert store.evidence_path(company).read_bytes() == upstream["evidence"]


def test_a_stale_upstream_fingerprint_prevents_a_resume(store: RunStore) -> None:
    """The point of the fingerprint: an existing report is not evidence of a current one."""
    execute(store)
    dossier = store.read_evidence(store.read_candidates().candidates[0].company_id)
    # The evidence changed underneath the analysis, without the analysis report changing.
    store.write_evidence(dossier.model_copy(update={"claims": dossier.claims[:1]}))

    result = execute(store, offline=True)
    assert resumed(result)["evidence"] is True  # its own upstream is unchanged
    assert resumed(result)["analysis"] is False
    assert resumed(result)["recommend"] is False
    assert resumed(result)["ui"] is False


def test_an_artifact_written_outside_the_orchestrator_is_not_trusted(store: RunStore) -> None:
    """No fingerprint means unknown provenance, which is a reason to rerun, not to trust."""
    execute(store)
    report = store.read_enrichment_report()
    store.write_enrichment_report(report.model_copy(update={"upstream_fingerprint": None}))

    result = execute(store)
    assert resumed(result)["source"] is True
    assert resumed(result)["enrich"] is False


def test_a_run_that_lost_its_memos_rebuilds_them(store: RunStore) -> None:
    execute(store)
    store.ranking_path().unlink()

    result = execute(store, offline=True)
    assert resumed(result)["analysis"] is True
    assert resumed(result)["recommend"] is False
    assert store.ranking_path().is_file()


# -- stop and continue -------------------------------------------------------


def test_a_sourcing_failure_stops_the_run(store: RunStore) -> None:
    def broken() -> Any:
        raise HnError("the index returned HTTP 503")

    with pytest.raises(PipelineAbortedError, match="discovery failed"):
        run_pipeline(
            store=store,
            plan=plan(),
            provider=FakeProvider(),
            client_factory=broken,
            fetcher_factory=no_network,
        )
    assert not store.candidates_path().exists()


def test_zero_candidates_stops_the_run(store: RunStore) -> None:
    import httpx

    from vc_scout.net.hn import HnAlgoliaClient

    def empty_client() -> HnAlgoliaClient:
        transport = httpx.MockTransport(lambda _r: httpx.Response(200, json={"hits": []}))
        return HnAlgoliaClient(httpx.Client(transport=transport), sleep=lambda _s: None)

    with pytest.raises(PipelineAbortedError, match="no candidates"):
        run_pipeline(
            store=store,
            plan=plan(),
            provider=FakeProvider(),
            client_factory=empty_client,
            fetcher_factory=no_network,
        )


def test_candidate_level_failures_do_not_stop_the_run(store: RunStore) -> None:
    """One company failing extraction is a fact the run carries, not a reason to stop."""
    import httpx

    from vc_scout.net.http import SafeFetcher

    def partial_fetcher() -> SafeFetcher:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host.startswith("deskloop"):
                return httpx.Response(500, text="boom")
            return demo_fetcher().client.transport.handler(request)  # type: ignore[union-attr]

        return SafeFetcher(
            client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False),
            resolver=lambda _host: ["93.184.216.34"],
            sleep=lambda _s: None,
        )

    result = run_pipeline(
        store=store,
        plan=plan(),
        provider=FakeProvider(),
        client_factory=demo_client,
        fetcher_factory=partial_fetcher,
    )
    assert statuses(result)["enrich"] is PipelineStageStatus.PARTIAL
    assert statuses(result)["recommend"] is not PipelineStageStatus.FAILED
    # The candidate stays in the run with a recorded gap rather than disappearing.
    ids = {row.company_id for row in store.read_enrichment_report().candidates}
    assert "deskloop" in ids
    assert (store.site_dir / "index.html").is_file()


def test_a_run_level_provider_failure_stops_the_llm_stages(store: RunStore) -> None:
    provider = AbortingProvider()
    result = run_pipeline(
        store=store,
        plan=plan(),
        provider=provider,
        client_factory=demo_client,
        fetcher_factory=demo_fetcher,
    )
    by_stage = statuses(result)

    assert by_stage["enrich"] in (PipelineStageStatus.COMPLETED, PipelineStageStatus.PARTIAL)
    assert by_stage["evidence"] is PipelineStageStatus.FAILED
    assert by_stage["analysis"] is PipelineStageStatus.SKIPPED
    assert by_stage["recommend"] is PipelineStageStatus.SKIPPED
    assert by_stage["ui"] is PipelineStageStatus.SKIPPED
    # Exactly one request was spent before the run understood it would all fail the same way.
    assert provider.calls == 1
    assert "run-level provider failure" in result.report.resumability["analysis"]


# -- stopping short ----------------------------------------------------------


@pytest.mark.parametrize(
    ("stop_after", "expected"),
    [
        (PipelineStage.SOURCE, 1),
        (PipelineStage.ENRICH, 2),
        (PipelineStage.EVIDENCE, 3),
        (PipelineStage.ANALYSIS, 4),
        (PipelineStage.RECOMMEND, 5),
    ],
)
def test_stop_after_runs_exactly_that_far(
    store: RunStore, stop_after: PipelineStage, expected: int
) -> None:
    result = execute(store, stop_after=stop_after)

    assert [record.stage for record in result.report.stages] == STAGES[:expected]
    assert result.report.stopped_after is stop_after
    assert not (store.site_dir / "index.html").exists()


def test_stopping_short_then_continuing_resumes_what_was_done(store: RunStore) -> None:
    execute(store, stop_after=PipelineStage.EVIDENCE)
    result = execute(store)

    assert resumed(result)["source"] is True
    assert resumed(result)["evidence"] is True
    assert resumed(result)["analysis"] is False
    assert (store.site_dir / "index.html").is_file()
