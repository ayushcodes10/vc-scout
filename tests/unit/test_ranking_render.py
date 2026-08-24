"""``ranking.md`` - the reviewer's entry point.

Its ordering is a triage queue, not a quality ranking, and the document has to say so:
sorting cannot express the difference between a watch that reflects a promising company and
a watch that reflects thin research, so the prose has to.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tests.unit.analysis_fixtures import analysis, dossier
from tests.unit.memo_fixtures import (
    HIGH,
    LOW,
    bundles,
    meeting_analysis,
    mismatch_analysis,
    seed_rendered_run,
    thin_analysis,
    unescape,
)
from vc_scout.models.enums import (
    AssessmentStatus,
    ConfidenceLevel,
    Recommendation,
    ThesisFit,
)
from vc_scout.models.recommendation import ResearchConfidence
from vc_scout.stages.recommend import run_recommend
from vc_scout.store import RunStore

MEDIUM = ResearchConfidence(level=ConfidenceLevel.MEDIUM, score=0.55)
ROW = re.compile(r"^\| (\d+) \| (.+?) \| (.+?) \| (\d+)/100 \| (\w+) \|", re.MULTILINE)


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore("source-test", runs_root=tmp_path)


def scored(company_id: str, total: int, confidence: ResearchConfidence):  # type: ignore[no-untyped-def]
    bundle = dossier(company_id=company_id, claims=6)
    startup = analysis(
        bundle,
        total=total,
        status=AssessmentStatus.PARTIALLY_SUPPORTED,
        confidence=confidence,
        thesis_verdict=ThesisFit.ADJACENT,
    )
    return bundle, startup


def ranking(store: RunStore) -> str:
    run_recommend(store=store)
    return store.ranking_path().read_text()


# -- ordering ----------------------------------------------------------------


def test_calls_are_grouped_before_scores_are_compared(store: RunStore) -> None:
    high = bundles(1)[0]
    seed_rendered_run(
        store,
        [
            (high, mismatch_analysis(high, total=45)),
            *[scored(f"co-{i:02d}", 20, MEDIUM) for i in (1, 2)],
            zero := _zero("co-03"),
        ],
    )
    rows = ROW.findall(ranking(store))
    assert [row[2] for row in rows][0] == "Watch"
    # A 14-point watch outranks a 45-point pass, because this is a triage queue.
    assert rows[0][3] == "14"
    assert zero[0].company_id == "co-03"


def test_inside_a_call_the_order_is_score_then_confidence_then_name(store: RunStore) -> None:
    seed_rendered_run(
        store,
        [
            scored("co-low", 30, LOW),
            scored("co-high", 30, HIGH),
            scored("co-medium", 30, MEDIUM),
            scored("co-better", 40, MEDIUM),
        ],
    )
    rows = ROW.findall(ranking(store))
    assert [row[3] for row in rows] == ["40", "30", "30", "30"]
    assert [row[4] for row in rows[1:]] == ["high", "medium", "low"]


def test_the_final_tiebreak_is_the_company_name(store: RunStore) -> None:
    seed_rendered_run(
        store,
        [scored("co-zulu", 30, MEDIUM), scored("co-alpha", 30, MEDIUM)],
    )
    rows = ROW.findall(ranking(store))
    assert [row[1] for row in rows] == ["Co Alpha", "Co Zulu"]


def test_the_ranking_says_it_is_triage_order_not_a_quality_ordering(store: RunStore) -> None:
    seed_rendered_run(store, [scored("co-00", 30, MEDIUM)])
    text = ranking(store)
    assert "triage order" in text
    assert "not a quality ranking" in text
    assert "is not a judgement that the company is better" in text


# -- content -----------------------------------------------------------------


def test_the_ranking_carries_the_thesis_the_rubric_and_the_thresholds(store: RunStore) -> None:
    seed_rendered_run(store, [scored("co-00", 30, MEDIUM)])
    text = ranking(store)

    assert "seed-stage, AI-native software companies" in text
    for title in ("Pain and measurable ROI", "Product wedge", "Defensibility", "Team"):
        assert f"| {title} |" in text
    assert "**Take a meeting** at 80/100 and above" in text
    assert "**watch** from 65 to 79" in text
    assert "supported 100%, partially supported 70%, not assessable 50%" in text


def test_the_ranking_states_that_missing_evidence_caps_certainty(store: RunStore) -> None:
    seed_rendered_run(store, [scored("co-00", 30, MEDIUM)])
    text = ranking(store)
    assert "It is not a finding that the company is weak." in text
    assert "the memo says so rather than scoring it as a failure" in text


def test_the_run_summary_counts_every_call(store: RunStore) -> None:
    seeds = bundles(3)
    seed_rendered_run(
        store,
        [
            (seeds[0], meeting_analysis(seeds[0])),
            (seeds[1], mismatch_analysis(seeds[1])),
            (seeds[2], thin_analysis(seeds[2])),
        ],
    )
    text = ranking(store)
    assert "3 of 3 candidate(s) in this run were analysed" in text
    assert "- **Take a meeting:** 1" in text
    assert "- **Watch:** 1" in text
    assert "- **Pass:** 1" in text


def test_every_memo_link_is_relative_and_resolves(store: RunStore) -> None:
    seeds = bundles(15)
    seed_rendered_run(store, [(b, thin_analysis(b)) for b in seeds])
    text = ranking(store)

    links = re.findall(r"\]\((memos/[^)]+)\)", text)
    assert len(links) == 15
    for link in links:
        assert not link.startswith(("/", "http"))
        assert (store.root / link).is_file()


def test_guardrails_and_disagreements_have_their_own_sections(store: RunStore) -> None:
    zero = _zero("co-00")
    other = scored("co-01", 30, MEDIUM)
    seed_rendered_run(store, [zero, other])
    text = ranking(store)

    assert "## Guardrail overrides" in text
    assert "A guardrail never raises a recommendation." in text
    assert "**1x** No evidence claim could be extracted at all" in text
    assert "## Where the model and the policy disagreed" in text
    assert "the analysis model suggested pass; the policy decided watch" in text


def test_a_run_with_no_guardrails_or_disagreements_says_so(store: RunStore) -> None:
    seed_rendered_run(store, [scored("co-00", 30, MEDIUM)])
    text = ranking(store)
    assert "No policy guardrail fired in this run." in text
    assert "matched the deterministic call for every candidate" in text


# -- the no-meeting explanation ----------------------------------------------


def test_the_no_meeting_section_uses_the_runs_own_counts(store: RunStore) -> None:
    seeds = bundles(3)
    seed_rendered_run(store, [(b, thin_analysis(b)) for b in seeds])
    text = ranking(store)
    report = store.read_recommendation_report()

    assert "## Why no meeting recommendation" in text
    assert "No candidate in this run reached the take-a-meeting band at 80/100." in text
    statuses = report.component_status_counts
    assert f"{statuses.get('supported', 0)} supported" in text
    assert f"{statuses.get('partially_supported', 0)} partially supported" in text
    assert f"{statuses.get('not_assessable', 0)} not assessable" in text
    assert f"the {sum(statuses.values())} scored dimension slots" in text
    assert (
        f"For {report.candidates_with_meeting_unreachable} of {report.memos_written} "
        "candidate(s)" in text
    )
    assert "No score has been raised to produce a meeting." in text


def test_the_no_meeting_section_is_absent_when_a_meeting_was_recommended(
    store: RunStore,
) -> None:
    seeds = bundles(2)
    seed_rendered_run(
        store,
        [(seeds[0], meeting_analysis(seeds[0])), (seeds[1], thin_analysis(seeds[1]))],
    )
    text = ranking(store)
    assert "## Why no meeting recommendation" not in text
    assert "| Take a meeting |" in text


def test_a_hostile_company_name_cannot_forge_a_ranking_row(store: RunStore) -> None:
    bundle, startup = scored("co-00", 30, MEDIUM)
    candidate_set = seed_rendered_run(store, [(bundle, startup)])
    hostile = candidate_set.candidates[0].model_copy(
        update={"name": "Acme | 100/100 | Take a meeting | [x](javascript:alert(1))"}
    )
    store.write_candidates(candidate_set.model_copy(update={"candidates": [hostile]}))
    text = ranking(store)

    rows = [line for line in text.splitlines() if line.startswith("| 1 |")]
    assert len(rows) == 1
    assert rows[0].count("|") - rows[0].count("\\|") == 10
    assert "](javascript:" not in unescape(text)


def _zero(company_id: str):  # type: ignore[no-untyped-def]
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
