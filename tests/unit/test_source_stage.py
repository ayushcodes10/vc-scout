"""The sourcing stage, end to end and offline.

Two kinds of test live here. The committed-fixture tests exercise a realistic funnel: a
handful of low-engagement workflow products, some heavily upvoted generic agent
infrastructure, and the usual noise. The synthetic tests control the exact supply of
direct and adjacent candidates so shortlist composition can be pinned precisely.

Nothing here opens a socket; ``tests/conftest.py`` blocks that for the whole suite.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests.unit.hn_fixtures import (
    NOW,
    QUERY,
    adjacent_story,
    direct_story,
    load_fixture,
    make_client,
    make_transport,
    story,
    wrap,
)
from vc_scout.discovery import MIN_RELEVANCE_SCORE, RejectionReason
from vc_scout.models.candidate import CandidateSet
from vc_scout.models.enums import RelevanceClass, SourceKind, TractionKind
from vc_scout.models.report import SourceReport
from vc_scout.net.hn import HnAlgoliaClient, query_variants
from vc_scout.stages.source import ADJACENT_MAX_SHARE, SourceOutcome, run_source
from vc_scout.store import RunStore

CORPUS = (
    "intent-smb",
    "intent-customer-support",
    "intent-finance-accounting",
    "query-story",
    "query-show-hn",
)


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore("source-test", runs_root=tmp_path)


def run(store: RunStore, responses: dict[str, Any], *, limit: int = 15, **kw: Any) -> SourceOutcome:
    return run_source(
        store=store, client=make_client(responses, **kw), query=QUERY, limit=limit, now=NOW
    )


def corpus() -> dict[str, Any]:
    """The committed, realistic fixture set."""
    return {label: load_fixture(label) for label in CORPUS}


def synthetic(direct: int, adjacent: int, *, irrelevant: int = 0, **kw: Any) -> dict[str, Any]:
    """A controlled supply of candidates, one domain each."""
    hits: list[Any] = [direct_story(i, **kw) for i in range(1, direct + 1)]
    hits += [adjacent_story(i, **kw) for i in range(1, adjacent + 1)]
    hits += [
        story(
            f"9{i:06d}",
            f"Show HN: Knitting {i} - a pattern generator",
            f"https://knit-{i}.example/",
            points=5000,
            comments=2000,
        )
        for i in range(1, irrelevant + 1)
    ]
    return {"intent-smb": wrap(hits)}


def classes(outcome: SourceOutcome) -> list[str]:
    return [
        c.discovery_rank.relevance_class.value
        for c in outcome.candidates.candidates
        if c.discovery_rank
    ]


# -- the regression this rework exists for -----------------------------------


def test_a_quiet_workflow_product_outranks_loud_generic_infrastructure(store: RunStore) -> None:
    """Acme Ops has 9 points and names a workflow; AgentMesh has 900 and names none."""
    outcome = run(store, corpus())
    ids = [c.company_id for c in outcome.candidates.candidates]

    assert ids.index("acme-ops") < ids.index("agentmesh")
    acme = next(c for c in outcome.candidates.candidates if c.company_id == "acme-ops")
    mesh = next(c for c in outcome.candidates.candidates if c.company_id == "agentmesh")
    assert acme.discovery_rank and mesh.discovery_rank
    # The adjacent candidate genuinely wins on engagement and still loses overall.
    assert mesh.discovery_rank.quality_score > acme.discovery_rank.quality_score
    assert acme.discovery_rank.relevance_class is RelevanceClass.DIRECT
    assert mesh.discovery_rank.relevance_class is RelevanceClass.ADJACENT


def test_every_direct_candidate_precedes_every_adjacent_one(store: RunStore) -> None:
    ordered = classes(run(store, corpus()))
    assert ordered == sorted(ordered, key=lambda c: c != "direct")
    assert "direct" in ordered and "adjacent" in ordered


def test_engagement_breaks_ties_between_equally_relevant_candidates(store: RunStore) -> None:
    """Quality is still doing work - just only within a relevance band."""
    quiet = direct_story(1, points=5, comments=1)
    loud = direct_story(2, points=500, comments=200)
    outcome = run(store, {"intent-smb": wrap([quiet, loud])}, limit=5)

    ranks = [c.discovery_rank for c in outcome.candidates.candidates]
    assert [r.relevance_class.value for r in ranks if r] == ["direct", "direct"]
    assert [r.relevance_score for r in ranks if r][0] == [r.relevance_score for r in ranks if r][1]
    assert [c.company_id for c in outcome.candidates.candidates] == ["direct-2", "direct-1"]


# -- eligibility -------------------------------------------------------------


def test_irrelevant_candidates_are_removed_before_truncation(store: RunStore) -> None:
    """Filtering after the cut would let noise occupy slots and shorten the shortlist."""
    outcome = run(store, synthetic(direct=3, adjacent=0, irrelevant=5), limit=5)

    assert [c.company_id for c in outcome.candidates.candidates] == [
        "direct-1",
        "direct-2",
        "direct-3",
    ]
    assert outcome.report.counts[f"rejected_{RejectionReason.IRRELEVANT}"] == 5
    assert RelevanceClass.IRRELEVANT.value not in classes(outcome)
    # All five were dropped for topic, none for the limit.
    assert f"rejected_{RejectionReason.OVER_LIMIT}" not in outcome.report.counts


def test_a_non_ai_business_tool_is_discarded_not_shortlisted(store: RunStore) -> None:
    outcome = run(store, corpus())
    assert "papertrail" not in [c.company_id for c in outcome.candidates.candidates]
    discarded = {d.object_id: d for d in outcome.report.discarded}
    assert discarded["50000005"].reason == RejectionReason.IRRELEVANT
    assert "AI-automation signal" in (discarded["50000005"].detail or "")


def test_the_minimum_relevance_threshold_is_applied_and_reported(store: RunStore) -> None:
    report = run(store, corpus()).report
    assert report.minimum_relevance == MIN_RELEVANCE_SCORE
    for candidate in run(store, corpus(), limit=15).candidates.candidates:
        assert candidate.discovery_rank
        assert candidate.discovery_rank.relevance_score >= MIN_RELEVANCE_SCORE


# -- shortlist composition ---------------------------------------------------


def test_adjacent_candidates_are_capped_when_direct_supply_is_sufficient(store: RunStore) -> None:
    outcome = run(store, synthetic(direct=7, adjacent=10), limit=10)
    counts = classes(outcome)

    assert len(counts) == 10
    assert counts.count("adjacent") == 3
    assert counts.count("adjacent") / len(counts) <= ADJACENT_MAX_SHARE
    assert outcome.report.counts[f"rejected_{RejectionReason.ADJACENT_SHARE}"] == 7


def test_direct_candidates_fill_the_shortlist_before_any_adjacent_one(store: RunStore) -> None:
    outcome = run(store, synthetic(direct=20, adjacent=20), limit=10)
    assert classes(outcome) == ["direct"] * 10
    assert outcome.report.counts[f"rejected_{RejectionReason.ADJACENT_SHARE}"] == 20


def test_adjacent_candidates_fill_the_rest_when_direct_supply_runs_out(store: RunStore) -> None:
    outcome = run(store, synthetic(direct=4, adjacent=10), limit=10)
    counts = classes(outcome)

    assert len(counts) == 10
    assert counts.count("direct") == 4
    # The share cap does not apply once direct candidates are exhausted.
    assert counts.count("adjacent") == 6
    assert counts.count("adjacent") / len(counts) > ADJACENT_MAX_SHARE
    # They lost to the limit, not to the share policy, and the report says which.
    assert f"rejected_{RejectionReason.ADJACENT_SHARE}" not in outcome.report.counts
    assert outcome.report.counts[f"rejected_{RejectionReason.OVER_LIMIT}"] == 4


def test_a_shortfall_is_reported_rather_than_padded(store: RunStore) -> None:
    outcome = run(store, synthetic(direct=2, adjacent=1, irrelevant=12), limit=10)

    assert len(outcome.candidates.candidates) == 3
    assert outcome.report.shortfall == 7
    assert RelevanceClass.IRRELEVANT.value not in classes(outcome)
    assert outcome.candidates.notes
    assert "not padded" in outcome.candidates.notes[0]
    assert outcome.report.counts[f"rejected_{RejectionReason.IRRELEVANT}"] == 12


def test_no_shortfall_is_reported_when_the_limit_is_met(store: RunStore) -> None:
    outcome = run(store, synthetic(direct=20, adjacent=0), limit=10)
    assert outcome.report.shortfall == 0
    assert outcome.candidates.notes == []


# -- website normalisation ---------------------------------------------------


def test_a_blog_launch_url_is_kept_as_a_source_while_website_uses_the_origin(
    store: RunStore,
) -> None:
    outcome = run(store, corpus())
    acme = next(c for c in outcome.candidates.candidates if c.company_id == "acme-ops")
    index = outcome.candidates.source_index()

    assert acme.website == "https://acmeops.example/"
    launch = [index[sid] for sid in acme.source_ids if index[sid].kind is SourceKind.COMPANY_PAGE]
    assert len(launch) == 1
    assert launch[0].url == "https://acmeops.example/blog/launch-post"
    assert any("normalised to the site origin" in note for note in acme.notes)


def test_a_root_launch_url_is_recorded_unchanged(store: RunStore) -> None:
    outcome = run(store, corpus())
    ledgerly = next(c for c in outcome.candidates.candidates if c.company_id == "ledgerly")
    assert ledgerly.website == "https://www.ledgerly.example/"
    assert not any("normalised" in note for note in ledgerly.notes)


def test_every_candidate_carries_both_its_hn_thread_and_its_launch_url(store: RunStore) -> None:
    outcome = run(store, corpus())
    index = outcome.candidates.source_index()
    for candidate in outcome.candidates.candidates:
        kinds = {index[sid].kind for sid in candidate.source_ids}
        assert kinds == {SourceKind.HN_STORY, SourceKind.COMPANY_PAGE}
        thread = next(
            index[sid] for sid in candidate.source_ids if index[sid].kind is SourceKind.HN_STORY
        )
        assert thread.url.startswith("https://news.ycombinator.com/item?id=")


def test_points_comments_and_publication_date_are_captured(store: RunStore) -> None:
    outcome = run(store, corpus())
    index = outcome.candidates.source_index()
    shift = next(c for c in outcome.candidates.candidates if c.company_id == "shiftpilot")
    thread = next(index[s] for s in shift.source_ids if index[s].kind is SourceKind.HN_STORY)

    assert (thread.hn_points, thread.hn_num_comments) == (30, 9)
    assert thread.published_at is not None
    assert {s.kind for s in shift.traction_signals} == {
        TractionKind.HN_POINTS,
        TractionKind.HN_COMMENTS,
        TractionKind.LAUNCH_DATE,
    }


# -- funnel tolerance --------------------------------------------------------


def test_code_hosts_incumbents_and_text_posts_are_discarded(store: RunStore) -> None:
    counts = run(store, corpus()).report.counts
    assert counts[f"rejected_{RejectionReason.BLOCKED_DOMAIN}"] == 1
    assert counts[f"rejected_{RejectionReason.INCUMBENT}"] == 1
    assert counts[f"rejected_{RejectionReason.NO_URL}"] == 1


def test_malformed_hits_are_recorded_and_do_not_fail_the_run(store: RunStore) -> None:
    outcome = run(store, corpus())
    malformed = [d for d in outcome.report.discarded if d.reason == RejectionReason.MALFORMED]
    assert len(malformed) == 2
    assert all(d.detail for d in malformed)
    assert outcome.candidates.candidates


def test_a_story_returned_by_two_variants_is_counted_once(store: RunStore) -> None:
    outcome = run(store, corpus())
    assert outcome.report.counts[f"rejected_{RejectionReason.DUPLICATE_STORY}"] == 1
    ids = [c.company_id for c in outcome.candidates.candidates]
    assert ids.count("shiftpilot") == 1


def test_duplicate_domains_collapse_keeping_the_more_relevant_story(store: RunStore) -> None:
    weak = story(
        "60000001",
        "Show HN: Acme - an AI agent platform",
        "https://acme.example/",
        points=900,
        comments=400,
    )
    strong = story(
        "60000002",
        "Show HN: Acme - AI agent for invoicing for small business",
        "https://acme.example/product",
        points=5,
        comments=1,
    )
    outcome = run(store, {"intent-smb": wrap([weak, strong])}, limit=5)

    assert len(outcome.candidates.candidates) == 1
    kept = outcome.candidates.candidates[0]
    assert kept.discovery_rank and kept.discovery_rank.relevance_class is RelevanceClass.DIRECT
    assert outcome.report.counts[f"rejected_{RejectionReason.DUPLICATE_DOMAIN}"] == 1


def test_a_failing_query_variant_does_not_fail_the_run(store: RunStore) -> None:
    outcome = run(store, corpus(), overrides={"query-story": httpx.Response(500)})
    assert outcome.candidates.candidates
    assert any("query-story" in failure for failure in outcome.report.failures)
    variant = next(v for v in outcome.report.variants if v.label == "query-story")
    assert variant.error is not None


def test_a_response_without_a_hits_array_is_survivable(store: RunStore) -> None:
    outcome = run(
        store, corpus(), overrides={"intent-smb": httpx.Response(200, json={"nbHits": 0})}
    )
    assert any("no hits array" in failure for failure in outcome.report.failures)


def test_every_variant_failing_yields_an_empty_but_valid_run(store: RunStore) -> None:
    overrides = {v.label: httpx.Response(500) for v in query_variants(QUERY)}
    outcome = run(store, {}, overrides=overrides)
    assert outcome.candidates.candidates == []
    assert len(outcome.report.failures) == len(query_variants(QUERY))
    assert store.candidates_path().exists()


# -- persistence and reporting -----------------------------------------------


def test_every_query_variant_is_requested_and_recorded(store: RunStore) -> None:
    seen: list[httpx.Request] = []
    client = HnAlgoliaClient(
        httpx.Client(
            transport=make_transport(corpus(), record=seen), base_url="https://hn.algolia.test"
        )
    )
    outcome = run_source(store=store, client=client, query=QUERY, limit=15, now=NOW)

    expected = [v.label for v in query_variants(QUERY)]
    assert len(seen) == len(expected)
    assert [v.label for v in outcome.report.variants] == expected
    assert len(expected) == 12


def test_raw_responses_are_persisted_verbatim(store: RunStore) -> None:
    outcome = run(store, corpus())
    raw_dir = store.resolve("raw", "hn")
    assert json.loads((raw_dir / "intent-smb-p0.json").read_text()) == load_fixture("intent-smb")
    for variant in outcome.report.variants:
        for path in variant.raw_paths:
            assert not Path(path).is_absolute()


def test_the_report_explains_the_whole_funnel(store: RunStore) -> None:
    report = run(store, corpus()).report

    assert report.formula_version is not None and report.formula_version != "1.0.0"
    assert report.ordering_policy and "lexicographic" in report.ordering_policy
    assert report.minimum_relevance == MIN_RELEVANCE_SCORE

    before, after = report.relevance_before_selection, report.relevance_after_selection
    assert set(before) == set(after) == {"direct", "adjacent", "irrelevant"}
    assert before["irrelevant"] > 0, "the report must show what was dropped, not only what survived"
    assert after["irrelevant"] == 0
    assert after["direct"] + after["adjacent"] == len(run(store, corpus()).candidates.candidates)
    assert report.counts["candidates_direct"] == after["direct"]
    assert all(d.reason for d in report.discarded)


def test_artifacts_round_trip_through_the_store(store: RunStore) -> None:
    outcome = run(store, corpus())
    assert store.read_candidates() == outcome.candidates
    assert store.read_source_report() == outcome.report
    assert isinstance(store.read_candidates(), CandidateSet)
    assert isinstance(store.read_source_report(), SourceReport)


def test_the_run_is_deterministic(tmp_path: Path) -> None:
    first, second = RunStore("run-a", runs_root=tmp_path), RunStore("run-b", runs_root=tmp_path)
    run(first, corpus())
    run(second, corpus())
    assert first.candidates_path().read_text().replace("run-a", "R") == (
        second.candidates_path().read_text().replace("run-b", "R")
    )


def test_no_absolute_paths_leak_into_artifacts(store: RunStore) -> None:
    run(store, corpus())
    for path in (store.candidates_path(), store.source_report_path()):
        assert str(store.root) not in path.read_text()


def test_matched_terms_are_recorded_on_every_candidate(store: RunStore) -> None:
    """A partner should be able to see why a candidate was called relevant."""
    for candidate in run(store, corpus()).candidates.candidates:
        assert candidate.discovery_rank
        assert candidate.discovery_rank.matched["ai"], candidate.company_id
        assert any("Relevance classified" in note for note in candidate.notes)
