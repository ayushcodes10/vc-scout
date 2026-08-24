"""Artifact storage: containment, atomicity and round-tripping."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.unit import analysis_fixtures, factories
from vc_scout.models.candidate import Candidate, CandidateSet
from vc_scout.models.enums import ConfidenceLevel, StageName, StageStatus
from vc_scout.models.manifest import RunManifest, StageRecord
from vc_scout.models.recommendation import ResearchConfidence
from vc_scout.policy import decide
from vc_scout.store import RunStore, StoreError


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore("demo", runs_root=tmp_path)


@pytest.mark.parametrize("bad", ["../escape", "has space", "/absolute", ""])
def test_invalid_run_ids_are_refused(tmp_path: Path, bad: str) -> None:
    with pytest.raises(StoreError):
        RunStore(bad, runs_root=tmp_path)


@pytest.mark.parametrize("bad", ["../../etc/passwd", "..", "a/../../b"])
def test_paths_cannot_escape_the_run_directory(store: RunStore, bad: str) -> None:
    with pytest.raises(StoreError, match="escapes the run directory"):
        store.resolve(bad)


@pytest.mark.parametrize("bad", ["../evil", "Has Space", "up/down"])
def test_company_scoped_paths_validate_the_company_id(store: RunStore, bad: str) -> None:
    with pytest.raises(StoreError, match="invalid company id"):
        store.evidence_path(bad)


def test_candidates_round_trip(store: RunStore) -> None:
    src = factories.source()
    original = CandidateSet(
        run_id="demo",
        query="AI agents for SMB operations",
        sources=[src],
        candidates=[Candidate(company_id="acme-ops", name="Acme Ops", source_ids=[src.source_id])],
    )
    store.write_candidates(original)
    assert store.read_candidates() == original


def test_evidence_round_trip(store: RunStore) -> None:
    original = factories.dossier()
    store.write_evidence(original)
    assert store.read_evidence(original.company_id) == original


def test_analysis_and_recommendation_are_separate_keys(store: RunStore) -> None:
    bundle = analysis_fixtures.dossier()
    subject = analysis_fixtures.analysis(bundle, total=70)
    recommendation = decide(
        subject, bundle, ResearchConfidence(level=ConfidenceLevel.HIGH, score=0.9)
    )
    path = store.write_analysis(subject, recommendation)

    payload = path.read_text(encoding="utf-8")
    assert '"analysis"' in payload
    assert '"recommendation"' in payload

    read_analysis, read_recommendation = store.read_analysis(subject.company_id)
    assert read_analysis == subject
    assert read_recommendation == recommendation


def test_analysis_may_be_persisted_before_the_policy_runs(store: RunStore) -> None:
    subject = analysis_fixtures.analysis(analysis_fixtures.dossier(), total=30)
    store.write_analysis(subject)
    _, recommendation = store.read_analysis(subject.company_id)
    assert recommendation is None


def test_a_failed_candidate_does_not_retain_a_stale_analysis(store: RunStore) -> None:
    """The same narrow cleanup contract as the evidence stage."""
    subject = analysis_fixtures.analysis(analysis_fixtures.dossier(), total=30)
    store.write_analysis(subject)
    assert store.analysis_company_ids() == [subject.company_id]

    assert store.delete_analysis(subject.company_id) is True
    assert store.analysis_company_ids() == []
    # Idempotent when nothing is there.
    assert store.delete_analysis(subject.company_id) is False


@pytest.mark.parametrize("unsafe", ["../escape", "..", "Has Space", "a/../../b"])
def test_delete_analysis_is_confined_to_a_validated_company_path(
    store: RunStore, unsafe: str
) -> None:
    with pytest.raises(StoreError):
        store.delete_analysis(unsafe)


def test_reading_a_missing_artifact_names_it(store: RunStore) -> None:
    with pytest.raises(StoreError, match="missing"):
        store.read_candidates()


def test_written_json_is_sorted_and_newline_terminated(store: RunStore) -> None:
    store.write_evidence(factories.dossier())
    text = store.evidence_path(factories.COMPANY_ID).read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert text.index('"claims"') < text.index('"company_id"') < text.index('"sources"')


def test_no_temporary_files_survive_a_write(store: RunStore) -> None:
    store.write_evidence(factories.dossier())
    assert list(store.resolve("evidence").glob(".*.tmp")) == []


def test_relative_paths_are_recorded_not_absolute_ones(store: RunStore) -> None:
    path = store.write_evidence(factories.dossier())
    assert store.relative(path) == f"evidence/{factories.COMPANY_ID}.json"


def test_manifest_round_trips_and_accumulates_stages(store: RunStore) -> None:
    manifest = RunManifest(run_id="demo", query="q")
    manifest.stages.append(StageRecord(name=StageName.SOURCE, status=StageStatus.OK))
    store.write_manifest(manifest)

    restored = store.read_manifest()
    assert restored.stage(StageName.SOURCE) is not None
    assert restored.stage(StageName.ENRICH) is None


def test_company_ids_lists_analysed_companies_in_stable_order(store: RunStore) -> None:
    assert store.company_ids() == []
    for company_id in ("zeta-co", "acme-ops"):
        bundle = analysis_fixtures.dossier(company_id=company_id)
        store.write_analysis(analysis_fixtures.analysis(bundle, total=10))
    assert store.company_ids() == ["acme-ops", "zeta-co"]
    assert store.analysis_company_ids() == ["acme-ops", "zeta-co"]
