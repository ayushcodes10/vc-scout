"""The recommendation stage: memos, the ranking, and the report that ties them together.

Rendering is the one stage with no judgement in it, which makes its correctness entirely a
question of fidelity: does the document say what the artifacts say, and does it keep saying
it byte for byte on a re-run.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.unit.analysis_fixtures import analysis, dossier
from tests.unit.memo_fixtures import (
    LOW,
    bundles,
    meeting_analysis,
    mismatch_analysis,
    seed_rendered_run,
    thin_analysis,
)
from vc_scout.models.enums import AssessmentStatus, ConfidenceLevel, Recommendation, ThesisFit
from vc_scout.models.recommendation import ResearchConfidence
from vc_scout.policy import TAKE_A_MEETING_AT
from vc_scout.render.engine import TEMPLATE_VERSION
from vc_scout.stages.recommend import MissingArtifactError, run_recommend
from vc_scout.store import RunStore

MEDIUM = ResearchConfidence(level=ConfidenceLevel.MEDIUM, score=0.5)


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore("source-test", runs_root=tmp_path)


def zero_claim(company_id: str):  # type: ignore[no-untyped-def]
    bundle = dossier(company_id=company_id, claims=0, unknowns=3)
    startup = analysis(
        bundle,
        total=14,
        status=AssessmentStatus.NOT_ASSESSABLE,
        confidence=LOW,
        thesis_verdict=ThesisFit.UNDETERMINED,
        thesis_evidence=False,
        suggested=Recommendation.PASS,
    )
    return bundle, startup


# -- artifacts ---------------------------------------------------------------


def test_the_stage_writes_exactly_the_three_required_outputs(store: RunStore) -> None:
    seeds = bundles(3)
    seed_rendered_run(store, [(b, mismatch_analysis(b)) for b in seeds])
    outcome = run_recommend(store=store)

    assert store.ranking_path().is_file()
    assert store.recommendation_report_path().is_file()
    assert store.memo_company_ids() == ["co-00", "co-01", "co-02"]
    assert outcome.ranking_path == "ranking.md"
    assert outcome.report_path == "recommendation-report.json"
    assert outcome.memo_paths == ["memos/co-00.md", "memos/co-01.md", "memos/co-02.md"]


def test_rendering_is_byte_identical_on_a_re_run(store: RunStore) -> None:
    seeds = bundles(5)
    seed_rendered_run(store, [(b, thin_analysis(b)) for b in seeds])
    run_recommend(store=store)
    first = {
        "ranking": store.ranking_path().read_bytes(),
        "report": store.recommendation_report_path().read_bytes(),
        **{b.company_id: store.memo_path(b.company_id).read_bytes() for b in seeds},
    }

    run_recommend(store=store)
    assert store.ranking_path().read_bytes() == first["ranking"]
    assert store.recommendation_report_path().read_bytes() == first["report"]
    for bundle in seeds:
        assert store.memo_path(bundle.company_id).read_bytes() == first[bundle.company_id]


def test_no_output_carries_a_generated_timestamp(store: RunStore) -> None:
    seeds = bundles(2)
    seed_rendered_run(store, [(b, mismatch_analysis(b)) for b in seeds])
    run_recommend(store=store)

    stamp = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
    assert not stamp.search(store.ranking_path().read_text())
    assert not stamp.search(store.recommendation_report_path().read_text())
    for bundle in seeds:
        # Observation dates are facts about a source and are kept; a *generation* time is
        # not a fact about the analysis and would break byte-for-byte replay.
        assert not stamp.search(store.memo_path(bundle.company_id).read_text())


# -- the report --------------------------------------------------------------


def test_the_report_reconciles_with_what_was_rendered(store: RunStore) -> None:
    seeds = bundles(4)
    specs = [
        (seeds[0], meeting_analysis(seeds[0])),
        (seeds[1], mismatch_analysis(seeds[1])),
        (seeds[2], thin_analysis(seeds[2])),
        (seeds[3], mismatch_analysis(seeds[3])),
    ]
    seed_rendered_run(store, specs)
    report = run_recommend(store=store).report

    assert report.run_id == "source-test"
    assert report.template_version == TEMPLATE_VERSION
    assert report.candidate_count == 4
    assert report.memos_written == 4
    assert sum(report.recommendations.values()) == 4
    assert report.recommendations["take_a_meeting"] == 1
    assert report.score_range == {
        "min": min(startup.total_score for _, startup in specs),
        "max": max(startup.total_score for _, startup in specs),
    }
    assert sum(report.component_status_counts.values()) == 4 * 7
    assert sorted(report.ordered_company_ids) == [b.company_id for b in seeds]
    assert [memo.company_id for memo in report.memos] == report.ordered_company_ids
    for memo in report.memos:
        assert memo.words > 0
        assert store.resolve(memo.memo_path).is_file()


def test_the_report_counts_the_headroom_that_put_a_meeting_out_of_reach(store: RunStore) -> None:
    seeds = bundles(3)
    seed_rendered_run(
        store,
        [
            (seeds[0], meeting_analysis(seeds[0])),
            (seeds[1], thin_analysis(seeds[1])),
            (seeds[2], thin_analysis(seeds[2])),
        ],
    )
    report = run_recommend(store=store).report

    assert report.candidates_with_meeting_unreachable == 2
    reachable = [m for m in report.memos if m.maximum_achievable_score >= TAKE_A_MEETING_AT]
    assert len(reachable) == 1


def test_disagreements_and_guardrails_are_counted(store: RunStore) -> None:
    bundle, startup = zero_claim("co-00")
    other = dossier(company_id="co-01", claims=6)
    seed_rendered_run(store, [(bundle, startup), (other, mismatch_analysis(other))])
    report = run_recommend(store=store).report

    assert report.model_policy_disagreements == 1
    assert report.guardrail_counts == {"zero_claim_dossier": 1}


# -- per-candidate failure ---------------------------------------------------


def test_a_candidate_without_an_analysis_is_recorded_and_the_rest_still_render(
    store: RunStore,
) -> None:
    seeds = bundles(3)
    seed_rendered_run(
        store,
        [
            (seeds[0], mismatch_analysis(seeds[0])),
            (seeds[1], None),
            (seeds[2], mismatch_analysis(seeds[2])),
        ],
    )
    outcome = run_recommend(store=store)

    assert outcome.report.memos_written == 2
    assert [f.company_id for f in outcome.report.failures] == ["co-01"]
    assert "no analysis" in outcome.report.failures[0].reason
    assert not store.memo_path("co-01").exists()
    assert store.memo_path("co-00").is_file()
    # The ranking names the gap rather than pretending the candidate was not in the run.
    assert "Candidates without a memo" in store.ranking_path().read_text()
    assert "co-01" in store.ranking_path().read_text()


def test_a_stale_memo_is_removed_when_this_run_cannot_render_one(store: RunStore) -> None:
    seeds = bundles(2)
    seed_rendered_run(store, [(b, mismatch_analysis(b)) for b in seeds])
    run_recommend(store=store)
    assert store.memo_path("co-01").is_file()

    # The analysis disappears - the candidate failed on a later run.
    store.delete_analysis("co-01")
    outcome = run_recommend(store=store)

    assert not store.memo_path("co-01").exists()
    assert [f.company_id for f in outcome.report.failures] == ["co-01"]
    assert any("removed a memo left over" in warning for warning in outcome.report.warnings)
    assert store.memo_path("co-00").is_file()


def test_an_analysis_whose_evidence_is_missing_does_not_render(store: RunStore) -> None:
    seeds = bundles(2)
    seed_rendered_run(store, [(b, mismatch_analysis(b)) for b in seeds])
    store.delete_evidence("co-00")
    outcome = run_recommend(store=store)

    assert [f.company_id for f in outcome.report.failures] == ["co-00"]
    assert "evidence" in outcome.report.failures[0].reason
    assert outcome.report.memos_written == 1


def test_a_candidate_never_analysed_at_all_is_a_recorded_failure(store: RunStore) -> None:
    seeds = bundles(1)
    seed_rendered_run(store, [(seeds[0], None)])
    outcome = run_recommend(store=store)

    assert outcome.report.memos_written == 0
    assert outcome.report.score_range == {}
    assert outcome.report.failures[0].company_id == "co-00"
    # The ranking still renders: a run where nothing could be analysed is still a result.
    assert store.ranking_path().is_file()


# -- upstream requirements ---------------------------------------------------


def test_a_run_without_candidates_is_a_run_level_failure(store: RunStore) -> None:
    with pytest.raises(MissingArtifactError, match="candidates.json"):
        run_recommend(store=store)


def test_a_run_that_was_never_analysed_is_a_run_level_failure(store: RunStore) -> None:
    seeds = bundles(1)
    seed_rendered_run(store, [(seeds[0], mismatch_analysis(seeds[0]))])
    store.analysis_report_path().unlink()
    with pytest.raises(MissingArtifactError, match="analysis-report.json"):
        run_recommend(store=store)


def test_rendering_works_without_any_extracted_pages(store: RunStore) -> None:
    """Enrichment may have failed entirely. The dossier still carries its own sources."""
    seeds = bundles(2)
    seed_rendered_run(store, [(b, mismatch_analysis(b)) for b in seeds], write_pages=False)
    outcome = run_recommend(store=store)

    assert outcome.report.memos_written == 2
    memo = store.read_memo("co-00")
    assert "## Sources" in memo
    assert "https://" in memo


def test_no_provider_or_credential_is_needed_to_render(
    store: RunStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The suite already strips keys; this asserts the stage never reaches for one."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    seeds = bundles(2)
    seed_rendered_run(store, [(b, mismatch_analysis(b)) for b in seeds])
    assert run_recommend(store=store).report.memos_written == 2


def test_the_suite_itself_cannot_reach_the_network(store: RunStore) -> None:
    """Proof that the offline guarantee is enforced, not merely intended."""
    import socket

    with pytest.raises(RuntimeError, match="network access is disabled"):
        socket.create_connection(("example.com", 443))

    seeds = bundles(1)
    seed_rendered_run(store, [(seeds[0], mismatch_analysis(seeds[0]))])
    assert run_recommend(store=store).report.memos_written == 1


def test_a_memo_path_cannot_escape_the_run_directory(store: RunStore) -> None:
    from vc_scout.store import StoreError

    for unusable in ("../escape", "..", "a/../../b", "Has Space"):
        with pytest.raises(StoreError, match="invalid company id"):
            store.memo_path(unusable)


def test_deleting_a_memo_touches_nothing_else(store: RunStore) -> None:
    seeds = bundles(2)
    seed_rendered_run(store, [(b, mismatch_analysis(b)) for b in seeds])
    run_recommend(store=store)
    untouched = store.read_memo("co-01")

    assert store.delete_memo("co-00") is True
    assert store.delete_memo("co-00") is False
    assert store.read_memo("co-01") == untouched
    assert store.analysis_path("co-00").is_file()
    assert store.evidence_path("co-00").is_file()
