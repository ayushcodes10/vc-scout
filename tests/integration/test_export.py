"""The reviewer-ready export.

`demo/` is meant to be committed and read by someone who has not run anything. So the tests
here are about what it must never contain as much as what it must: no raw third-party HTML,
no credential-shaped string, no absolute path from the machine that built it, and no link
that leaves the directory.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from tests.integration.test_pipeline import execute
from vc_scout.stages.export import ExportError, export_demo
from vc_scout.store import RunStore

REQUIRED = (
    "README.md",
    "ranking.md",
    "artifacts/candidates.json",
    "artifacts/source-report.json",
    "artifacts/enrichment-report.json",
    "artifacts/evidence-report.json",
    "artifacts/analysis-report.json",
    "artifacts/recommendation-report.json",
    "artifacts/ui-report.json",
    "artifacts/run-report.json",
    "ai-trace/README.md",
    "ai-trace/selected-request.json",
    "ai-trace/selected-response.json",
    "ai-trace/validation-summary.md",
    "site/index.html",
    "site/assets/styles.css",
    "site/assets/app.js",
)

FORBIDDEN = (
    re.compile(r"sk-ant-"),
    re.compile(r"(?i)\b(?:x-api-key|authorization|set-cookie)\b\s*[:=]"),
    re.compile(r"(?i)\b[A-Z_]*API_KEY\b\s*[:=]"),
    re.compile(r"(?<![\w./])/(?:Users|home|root|var/folders)/"),
)


@pytest.fixture
def exported(tmp_path: Path) -> Path:
    store = RunStore("offline-demo", runs_root=tmp_path / "runs")
    execute(store)
    destination = tmp_path / "demo"
    export_demo(store=store, destination=destination)
    return destination


def test_the_export_contains_every_required_file(exported: Path) -> None:
    for name in REQUIRED:
        assert (exported / name).is_file(), name
    assert list((exported / "memos").glob("*.md"))
    assert list((exported / "site" / "companies").glob("*.html"))
    for directory in ("evidence", "analyses", "extracted"):
        assert list((exported / "artifacts" / directory).glob("*.json")), directory


def test_the_export_carries_no_raw_page_bodies(exported: Path) -> None:
    """`raw/` is the input to extraction, not evidence, and is deliberately left behind."""
    assert not (exported / "artifacts" / "raw").exists()
    stray = [
        path for path in exported.rglob("*.html") if path.parent.name not in {"site", "companies"}
    ]
    assert stray == []


def test_nothing_in_the_export_looks_like_a_credential_or_a_local_path(
    exported: Path,
) -> None:
    for path in exported.rglob("*"):
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        for pattern in FORBIDDEN:
            assert not pattern.search(text), f"{path.name}: {pattern.pattern}"


def test_every_link_inside_the_export_resolves(exported: Path) -> None:
    checked = 0
    for page in list(exported.rglob("*.html")) + list(exported.rglob("*.md")):
        text = page.read_text(encoding="utf-8")
        targets = re.findall(r'(?:href|src)="([^"]+)"', text) + re.findall(r"\]\(([^)]+)\)", text)
        for target in targets:
            if target.startswith(("http://", "https://", "#", "mailto:")):
                continue
            checked += 1
            assert (page.parent / target).resolve().exists(), f"{page.name} -> {target}"
    assert checked > 20


def test_the_ai_trace_is_the_richest_successful_candidate(tmp_path: Path) -> None:
    """Chosen by evidence weight, not by ranking position and not by name."""
    from vc_scout.stages.export import MIN_TRACE_CLAIMS

    store = RunStore("offline-demo", runs_root=tmp_path / "runs")
    execute(store)
    destination = tmp_path / "demo"
    result = export_demo(store=store, destination=destination)

    analysis = store.read_analysis_report()
    succeeded = [row for row in analysis.candidates if row.succeeded]
    substantial = [row for row in succeeded if row.evidence_claims >= MIN_TRACE_CLAIMS]
    expected = sorted(
        substantial or succeeded,
        key=lambda row: (-row.evidence_claims, -(row.total_score or 0), row.company_id),
    )[0]

    assert result.trace_company_id == expected.company_id
    request = json.loads((destination / "ai-trace" / "selected-request.json").read_text())
    assert expected.company_id in json.dumps(request)
    # It is not merely whichever candidate the ranking happens to lead with.
    chosen = next(r for r in succeeded if r.company_id == result.trace_company_id)
    assert chosen.evidence_claims == max(row.evidence_claims for row in succeeded)


def test_the_trace_readme_states_why_that_candidate_was_chosen(tmp_path: Path) -> None:
    store = RunStore("offline-demo", runs_root=tmp_path / "runs")
    execute(store)
    destination = tmp_path / "demo"
    result = export_demo(store=store, destination=destination)
    readme = (destination / "ai-trace" / "README.md").read_text()

    assert "## Why this candidate" in readme
    assert "Chosen by rule, not by hand" in readme
    assert "evidence claims" in readme
    assert str(result.trace_company_id) in readme


def test_the_trace_names_the_provider_prompt_and_validation_outcome(exported: Path) -> None:
    readme = (exported / "ai-trace" / "README.md").read_text()
    summary = (exported / "ai-trace" / "validation-summary.md").read_text()

    assert "provider / model:" in readme
    assert "prompt version:" in readme
    assert "thesis version:" in readme
    assert "attempts:" in readme
    assert "The model did not decide the outcome." in readme
    assert "| Attempt | Result | Error category | Validation errors |" in summary
    assert "recomputed from the components" in summary


def test_the_readme_explains_the_preview_and_the_citation_chain(exported: Path) -> None:
    readme = (exported / "README.md").read_text()
    assert "python3 -m http.server 8000 --directory demo/site" in readme
    assert "Trace one claim back to a source" in readme
    assert "artifacts/evidence/" in readme
    assert "deterministic policy" in readme


def test_the_export_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    store = RunStore("offline-demo", runs_root=tmp_path / "runs")
    execute(store)
    destination = tmp_path / "demo"
    export_demo(store=store, destination=destination)

    with pytest.raises(ExportError, match="already exists"):
        export_demo(store=store, destination=destination)

    keepsake = destination / "notes.txt"
    keepsake.write_text("mine")
    export_demo(store=store, destination=destination, force=True)
    assert (destination / "README.md").is_file()
    # --force replaces what the export owns, not the whole directory.
    assert keepsake.read_text() == "mine"


def test_exporting_a_run_that_was_never_rendered_is_refused(tmp_path: Path) -> None:
    store = RunStore("empty", runs_root=tmp_path / "runs")
    store.ensure_root()
    with pytest.raises(ExportError, match="nothing to\n?\\s*export|recommendation-report"):
        export_demo(store=store, destination=tmp_path / "demo")


def test_the_export_is_deterministic_apart_from_the_run_report(tmp_path: Path) -> None:
    store = RunStore("offline-demo", runs_root=tmp_path / "runs")
    execute(store)
    first = tmp_path / "a"
    second = tmp_path / "b"
    export_demo(store=store, destination=first)
    export_demo(store=store, destination=second)

    for path in sorted(first.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(first)
        assert path.read_bytes() == (second / relative).read_bytes(), relative
