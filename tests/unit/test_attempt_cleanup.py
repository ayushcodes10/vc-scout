"""Per-candidate cleanup of persisted LLM attempt files.

Regression cover for the live audit: after a run that needed fewer attempts than its
predecessor, ``llm/`` held 30 request and 30 response files against 20 recorded attempts.
Ten stale ``attempt2`` pairs recorded *failures* for candidates the report said succeeded on
the first try - which misread the run, and misled the audit script written to check it.

The rule these tests pin: **after a run, a candidate's attempt files on disk are exactly the
attempts the report records for it.**
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.unit.analysis_fixtures import NOW, analysis_payload, dossier, seed_run
from tests.unit.evidence_fixtures import claim_payload, source_ids
from tests.unit.evidence_fixtures import seed_run as seed_evidence
from vc_scout.llm.fake import FakeProvider
from vc_scout.llm.provider import LlmRequest, ModelConfig
from vc_scout.stages.analysis import run_analysis
from vc_scout.stages.evidence import run_evidence
from vc_scout.store import RunStore, StoreError

CONFIG = ModelConfig(model="fake-model-1", max_tokens=4096, effort="low")


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore("source-test", runs_root=tmp_path)


def attempt_files(store: RunStore, stage: str) -> dict[str, list[str]]:
    kinds = {
        "analysis": ("analysis-requests", "analysis-responses"),
        "evidence": ("evidence-requests", "evidence-responses"),
    }[stage]
    return {
        kind: sorted(p.name for p in store.resolve("llm", kind).glob("*.json")) for kind in kinds
    }


def recorded_attempts(report: Any) -> set[str]:
    return {
        f"{row.company_id}-attempt{a.attempt}.json"
        for row in report.candidates
        for a in row.attempts
    }


# -- the store primitive -----------------------------------------------------


def test_deletion_is_confined_to_one_validated_company(store: RunStore) -> None:
    seed_run(store, [dossier(company_id="co-00", claims=4), dossier(company_id="co-01", claims=4)])
    run_analysis(store=store, provider=FakeProvider(), config=CONFIG, now=NOW)
    assert len(attempt_files(store, "analysis")["analysis-requests"]) == 2

    assert store.delete_llm_attempts("co-00", stage="analysis") == 2
    remaining = attempt_files(store, "analysis")
    assert remaining["analysis-requests"] == ["co-01-attempt1.json"]
    assert remaining["analysis-responses"] == ["co-01-attempt1.json"]


@pytest.mark.parametrize("unsafe", ["../escape", "..", "Has Space", "a/../../b", "*"])
def test_deletion_rejects_an_unusable_company_id(store: RunStore, unsafe: str) -> None:
    with pytest.raises(StoreError, match="invalid company id"):
        store.delete_llm_attempts(unsafe, stage="analysis")


def test_deletion_rejects_an_unknown_stage(store: RunStore) -> None:
    with pytest.raises(StoreError, match="unknown stage"):
        store.delete_llm_attempts("co-00", stage="memos")


def test_deletion_is_idempotent_and_touches_nothing_else(store: RunStore) -> None:
    bundle = dossier(claims=4)
    seed_run(store, [bundle])
    run_analysis(store=store, provider=FakeProvider(), config=CONFIG, now=NOW)
    before = {
        "analysis": store.analysis_path(bundle.company_id).read_text(),
        "evidence": store.evidence_path(bundle.company_id).read_text(),
        "candidates": store.candidates_path().read_text(),
    }

    assert store.delete_llm_attempts(bundle.company_id, stage="analysis") == 2
    assert store.delete_llm_attempts(bundle.company_id, stage="analysis") == 0
    # Dossiers, analyses and source artifacts are never in scope.
    assert store.analysis_path(bundle.company_id).read_text() == before["analysis"]
    assert store.evidence_path(bundle.company_id).read_text() == before["evidence"]
    assert store.candidates_path().read_text() == before["candidates"]


# -- the analysis stage ------------------------------------------------------


def test_a_previous_attempt_two_is_removed_when_the_next_run_succeeds_first_try(
    store: RunStore,
) -> None:
    """The exact shape of the live defect."""
    bundle = dossier(claims=4)
    seed_run(store, [bundle])
    broken = analysis_payload(bundle, changers=0)
    run_analysis(store=store, provider=FakeProvider([broken, dict(broken)]), config=CONFIG, now=NOW)
    assert attempt_files(store, "analysis")["analysis-responses"] == [
        "acme-ops-attempt1.json",
        "acme-ops-attempt2.json",
    ]

    outcome = run_analysis(store=store, provider=FakeProvider(), config=CONFIG, now=NOW)
    assert outcome.report.candidates[0].succeeded is True
    assert len(outcome.report.candidates[0].attempts) == 1
    files = attempt_files(store, "analysis")
    assert files["analysis-requests"] == ["acme-ops-attempt1.json"]
    assert files["analysis-responses"] == ["acme-ops-attempt1.json"]
    assert outcome.report.counts["stale_attempts_removed"] == 4


def test_a_genuine_attempt_two_is_retained(store: RunStore) -> None:
    bundle = dossier(claims=4)
    seed_run(store, [bundle])
    broken = analysis_payload(bundle, changers=0)
    outcome = run_analysis(
        store=store,
        provider=FakeProvider([broken, analysis_payload(bundle)]),
        config=CONFIG,
        now=NOW,
    )
    assert [a.attempt for a in outcome.report.candidates[0].attempts] == [1, 2]
    assert attempt_files(store, "analysis")["analysis-responses"] == [
        "acme-ops-attempt1.json",
        "acme-ops-attempt2.json",
    ]


def test_disk_artifacts_match_the_report_exactly_after_a_full_run(store: RunStore) -> None:
    bundles = [dossier(company_id=f"co-{i:02d}", claims=4) for i in range(5)]
    seed_run(store, bundles)
    broken = analysis_payload(bundles[2], changers=0)

    def handler(request: LlmRequest) -> Any:
        if "company_id: co-02" in request.user_payload:
            return broken
        from vc_scout.llm.fake import derive_response

        return derive_response(request)

    outcome = run_analysis(
        store=store, provider=FakeProvider(handler=handler), config=CONFIG, now=NOW
    )
    expected = recorded_attempts(outcome.report)
    files = attempt_files(store, "analysis")
    assert set(files["analysis-requests"]) == expected
    assert set(files["analysis-responses"]) == expected


def test_another_candidates_attempt_files_stay_byte_identical(store: RunStore) -> None:
    bundles = [dossier(company_id=f"co-{i:02d}", claims=4) for i in range(3)]
    seed_run(store, bundles)
    run_analysis(store=store, provider=FakeProvider(), config=CONFIG, now=NOW)
    untouched = {
        name: (store.resolve("llm", "analysis-responses") / name).read_text()
        for name in ("co-01-attempt1.json", "co-02-attempt1.json")
    }

    run_analysis(
        store=store, provider=FakeProvider(), config=CONFIG, now=NOW, only_company_id="co-00"
    )
    for name, blob in untouched.items():
        assert (store.resolve("llm", "analysis-responses") / name).read_text() == blob


def test_a_filtered_run_cleans_only_the_selected_candidate(store: RunStore) -> None:
    bundles = [dossier(company_id=f"co-{i:02d}", claims=4) for i in range(3)]
    seed_run(store, bundles)
    broken = analysis_payload(bundles[0], changers=0)

    def handler(request: LlmRequest) -> Any:
        from vc_scout.llm.fake import derive_response

        if "company_id: co-00" in request.user_payload:
            return broken
        return derive_response(request)

    run_analysis(store=store, provider=FakeProvider(handler=handler), config=CONFIG, now=NOW)
    assert "co-00-attempt2.json" in attempt_files(store, "analysis")["analysis-responses"]

    run_analysis(
        store=store, provider=FakeProvider(), config=CONFIG, now=NOW, only_company_id="co-00"
    )
    files = attempt_files(store, "analysis")["analysis-responses"]
    # co-00's surplus attempt is gone; the other two keep exactly what they had.
    assert "co-00-attempt2.json" not in files
    assert files == ["co-00-attempt1.json", "co-01-attempt1.json", "co-02-attempt1.json"]


# -- the evidence stage ------------------------------------------------------


def test_the_evidence_stage_cleans_its_own_attempt_files(store: RunStore) -> None:
    """Stage 4 shared the defect, so it shares the fix."""
    seed_evidence(store)
    bad = {"claims": [claim_payload("src-ffffffffffff")], "unknowns": [], "conflicts": []}
    good = {
        "claims": [claim_payload(source_ids(store)["homepage"])],
        "unknowns": [],
        "conflicts": [],
    }
    run_evidence(store=store, provider=FakeProvider([bad, good]), config=CONFIG, now=NOW)
    assert attempt_files(store, "evidence")["evidence-responses"] == [
        "acme-ops-attempt1.json",
        "acme-ops-attempt2.json",
    ]

    outcome = run_evidence(store=store, provider=FakeProvider([good]), config=CONFIG, now=NOW)
    files = attempt_files(store, "evidence")
    assert files["evidence-requests"] == ["acme-ops-attempt1.json"]
    assert files["evidence-responses"] == ["acme-ops-attempt1.json"]
    assert set(files["evidence-responses"]) == recorded_attempts(outcome.report)


def test_cleaning_one_stage_leaves_the_other_stages_attempts_alone(store: RunStore) -> None:
    seed_evidence(store)
    good = {
        "claims": [claim_payload(source_ids(store)["homepage"])],
        "unknowns": [],
        "conflicts": [],
    }
    run_evidence(store=store, provider=FakeProvider([good]), config=CONFIG, now=NOW)
    evidence_before = attempt_files(store, "evidence")

    run_analysis(store=store, provider=FakeProvider(), config=CONFIG, now=NOW)
    assert attempt_files(store, "evidence") == evidence_before
    assert attempt_files(store, "analysis")["analysis-requests"] == ["acme-ops-attempt1.json"]


def test_a_not_attempted_candidate_owns_no_attempt_files(store: RunStore) -> None:
    """After a run-level abort, candidates never sent must hold no attempt artifacts."""
    from vc_scout.llm.provider import LlmError
    from vc_scout.models.enums import LlmErrorCategory

    bundles = [dossier(company_id=f"co-{i:02d}", claims=4) for i in range(4)]
    seed_run(store, bundles)
    run_analysis(store=store, provider=FakeProvider(), config=CONFIG, now=NOW)
    assert len(attempt_files(store, "analysis")["analysis-responses"]) == 4

    outcome = run_analysis(
        store=store,
        provider=FakeProvider(
            handler=lambda _r: LlmError(
                LlmErrorCategory.PROVIDER_HTTP_ERROR, "HTTP 400", status=400, run_level=True
            )
        ),
        config=CONFIG,
        now=NOW,
    )
    assert set(attempt_files(store, "analysis")["analysis-responses"]) == recorded_attempts(
        outcome.report
    )
    assert attempt_files(store, "analysis")["analysis-responses"] == ["co-00-attempt1.json"]
