"""The committed `demo/` directory, audited as an artifact in its own right.

`demo/` is the one directory in this repository that a reviewer opens before running
anything, and the only one where a mistake is permanent and public. It is produced by
`vc-scout export-demo` from a live run, so these tests are not about the exporter - they
are about the thing that was actually committed.

Skipped when `demo/` is absent, so a checkout that has not exported one still runs green.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

DEMO = Path(__file__).resolve().parents[2] / "demo"

pytestmark = pytest.mark.skipif(not DEMO.is_dir(), reason="no demo/ export in this checkout")

MARKER = re.compile(r"\[(S\d+)\]")
SOURCE_ENTRY = re.compile(r"^\*\*\[(S\d+)\]\*\*", re.MULTILINE)

FORBIDDEN = (
    ("credential", re.compile(r"sk-ant-[A-Za-z0-9_-]{4,}")),
    ("auth header", re.compile(r"(?i)\b(?:x-api-key|authorization|set-cookie)\b\s*[:=]")),
    ("environment dump", re.compile(r"(?i)\b[A-Z_]*API_KEY\b\s*[:=]")),
    ("absolute path", re.compile(r"(?<![\w./])/(?:Users|home|root|var/folders)/")),
)


def artifact(name: str) -> dict:
    return json.loads((DEMO / "artifacts" / name).read_text())


@pytest.fixture(scope="module")
def reports() -> dict[str, dict]:
    return {
        name: artifact(f"{name}-report.json")
        for name in ("source", "enrichment", "evidence", "analysis", "recommendation", "ui", "run")
    }


@pytest.fixture(scope="module")
def expected() -> int:
    return len(artifact("candidates.json")["candidates"])


# -- completeness ------------------------------------------------------------


def test_every_candidate_has_a_full_set_of_artifacts(expected: int) -> None:
    assert expected >= 1
    for directory, suffix in (
        ("artifacts/evidence", ".json"),
        ("artifacts/analyses", ".json"),
        ("artifacts/extracted", ".json"),
        ("memos", ".md"),
        ("site/companies", ".html"),
    ):
        found = list((DEMO / directory).glob(f"*{suffix}"))
        assert len(found) == expected, f"{directory}: {len(found)} of {expected}"


def test_no_candidate_failed(reports: dict[str, dict], expected: int) -> None:
    analysis = reports["analysis"]
    assert analysis["counts"]["failed"] == 0
    assert analysis["counts"]["succeeded"] == expected
    assert analysis["filtered_to"] is None
    assert reports["recommendation"]["failures"] == []
    assert reports["ui"]["failures"] == []


# -- reconciliation ----------------------------------------------------------


def test_recommendation_counts_agree_everywhere(reports: dict[str, dict], expected: int) -> None:
    counts = reports["analysis"]["recommendations"]
    assert counts == reports["recommendation"]["recommendations"]
    assert counts == reports["run"]["recommendations"]
    # The UI slugs its keys for CSS; the totals must still match.
    assert reports["ui"]["recommendations"] == {
        key.replace("_", "-"): value for key, value in counts.items()
    }
    assert sum(counts.values()) == expected


def test_the_dashboard_shows_exactly_those_calls(reports: dict[str, dict], expected: int) -> None:
    index = (DEMO / "site" / "index.html").read_text()
    assert len(re.findall(r"<tr data-company=", index)) == expected
    for slug, total in reports["ui"]["recommendations"].items():
        assert index.count(f'badge badge--{slug}">') == total


def test_the_ranking_lists_every_candidate_once(expected: int) -> None:
    ranking = (DEMO / "ranking.md").read_text()
    links = re.findall(r"\]\((memos/[^)]+)\)", ranking)
    assert len(links) == expected == len(set(links))


def test_analysis_aggregates_are_derived_from_their_own_outcomes(
    reports: dict[str, dict],
) -> None:
    rows = reports["analysis"]["candidates"]
    counts = reports["analysis"]["counts"]
    assert counts["candidates"] == len(rows)
    assert counts["attempts"] == sum(len(row["attempts"]) for row in rows)
    assert counts["input_tokens"] == sum(a["input_tokens"] for r in rows for a in r["attempts"])
    assert counts["output_tokens"] == sum(a["output_tokens"] for r in rows for a in r["attempts"])


# -- provenance --------------------------------------------------------------


def test_every_analysis_came_from_the_live_provider() -> None:
    for path in sorted((DEMO / "artifacts" / "analyses").glob("*.json")):
        analysis = json.loads(path.read_text())["analysis"]
        assert analysis["provider"] == "anthropic", path.name
        assert analysis["model"] == "claude-sonnet-5", path.name
        assert analysis["prompt_version"] in {"analysis_v2", "analysis_v2.1"}, path.name
    for path in sorted((DEMO / "artifacts" / "evidence").glob("*.json")):
        assert json.loads(path.read_text())["provider"] == "anthropic", path.name


def test_no_fake_provider_output_was_exported(reports: dict[str, dict]) -> None:
    for name in ("analysis", "evidence", "run"):
        assert '"fake"' not in json.dumps(reports[name])


# -- the citation chain ------------------------------------------------------


def test_every_memo_citation_resolves_to_exactly_one_source() -> None:
    for memo in sorted((DEMO / "memos").glob("*.md")):
        text = memo.read_text()
        used = set(MARKER.findall(text))
        listed = SOURCE_ENTRY.findall(text)
        assert used == set(listed), memo.name
        assert len(listed) == len(set(listed)), memo.name


def test_every_cited_evidence_id_exists_in_that_companys_dossier() -> None:
    for path in sorted((DEMO / "artifacts" / "analyses").glob("*.json")):
        analysis = json.loads(path.read_text())["analysis"]
        dossier = json.loads((DEMO / "artifacts" / "evidence" / path.name).read_text())
        known = {claim["claim_id"] for claim in dossier["claims"]}
        cited = {i for c in analysis["score_components"] for i in c["evidence_claim_ids"]}
        for key in ("team_assessment", "product_assessment", "market_assessment"):
            cited |= set(analysis[key]["evidence_claim_ids"])
        cited |= set(analysis["thesis_assessment"]["evidence_claim_ids"])
        assert not cited - known, f"{path.stem}: {sorted(cited - known)}"


def test_every_evidence_claim_resolves_to_a_source_the_dossier_carries() -> None:
    for path in sorted((DEMO / "artifacts" / "evidence").glob("*.json")):
        dossier = json.loads(path.read_text())
        known = {source["source_id"] for source in dossier["sources"]}
        for claim in dossier["claims"]:
            assert not set(claim["source_ids"]) - known, f"{path.stem}:{claim['claim_id']}"
            assert claim["excerpts"], claim["claim_id"]


# -- links -------------------------------------------------------------------


def test_every_internal_link_in_the_export_resolves() -> None:
    checked = 0
    for page in list(DEMO.rglob("*.html")) + list(DEMO.rglob("*.md")):
        text = page.read_text()
        targets = re.findall(r'(?:href|src)="([^"]+)"', text)
        targets += re.findall(r"\]\(([^)]+)\)", text)
        for target in targets:
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            checked += 1
            assert (page.parent / target).resolve().exists(), f"{page.name} -> {target}"
    assert checked > 50


def test_the_readme_uses_repository_relative_paths_only() -> None:
    readme = (DEMO / "README.md").read_text()
    assert not re.search(r"(?<![\w./])/(?:Users|home|root|var)/", readme)
    assert "demo/site" in readme
    assert "Trace one claim back to a source" in readme


# -- what must not be there --------------------------------------------------


def test_nothing_exported_carries_a_secret_or_a_local_path() -> None:
    for path in sorted(DEMO.rglob("*")):
        if not path.is_file():
            continue
        text = path.read_text(errors="replace")
        for label, pattern in FORBIDDEN:
            match = pattern.search(text)
            assert match is None, f"{path.relative_to(DEMO)}: {label} {match.group(0)[:24]!r}"


def test_no_raw_page_body_or_attempt_artifact_was_exported() -> None:
    assert not (DEMO / "artifacts" / "raw").exists()
    assert not (DEMO / "artifacts" / "llm").exists()
    assert [p.name for p in DEMO.rglob("*-attempt*.json")] == []
    stray = [p for p in DEMO.rglob("*.html") if "site" not in p.parts]
    assert stray == []


# -- the AI trace ------------------------------------------------------------


def test_the_trace_is_complete_and_explains_its_own_selection(reports: dict[str, dict]) -> None:
    for name in (
        "README.md",
        "selected-request.json",
        "selected-response.json",
        "validation-summary.md",
    ):
        assert (DEMO / "ai-trace" / name).is_file(), name

    readme = (DEMO / "ai-trace" / "README.md").read_text()
    assert "## Why this candidate" in readme
    assert "Chosen by rule, not by hand" in readme
    assert "The model did not decide the outcome." in readme

    # The chosen candidate is the one the documented rule selects.
    succeeded = [row for row in reports["analysis"]["candidates"] if row["succeeded"]]
    substantial = [row for row in succeeded if row["evidence_claims"] >= 3]
    chosen = sorted(
        substantial or succeeded,
        key=lambda row: (-row["evidence_claims"], -(row["total_score"] or 0), row["company_id"]),
    )[0]
    assert chosen["company_id"] in readme
    request = json.loads((DEMO / "ai-trace" / "selected-request.json").read_text())
    assert chosen["company_id"] in json.dumps(request)
    assert chosen["evidence_claims"] == max(row["evidence_claims"] for row in succeeded)
