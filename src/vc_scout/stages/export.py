"""The reviewer-ready export.

`demo/` is what a reviewer opens first, so it has to stand alone: the site, the memos, the
ranking, the artifacts behind them, and one worked example of the AI workflow. Everything
in it comes from a completed run - nothing is regenerated, reformatted or reworded here.

Three rules shape what is copied.

* **No raw HTML.** `raw/` holds third-party page bodies. They are the input to extraction,
  not evidence, and shipping them would put unreviewed third-party markup in a directory
  intended for a repository.
* **No credentials, and no absolute paths.** Persisted requests already record no header,
  so no key can be present; the export re-checks anyway, and refuses rather than ships.
* **One trace, chosen deterministically.** The highest-ranked candidate with a current,
  successful analysis - so the example a reviewer reads is the one the ranking put first,
  and running the export twice picks the same one.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from vc_scout.models.report import AnalysisOutcome
from vc_scout.store import RunStore, StoreError
from vc_scout.util.jsonio import dumps, read_json

__all__ = ["ExportError", "ExportResult", "export_demo"]

#: Copied as-is from the run root. Every one is a validated JSON artifact.
_ROOT_ARTIFACTS = (
    "candidates.json",
    "source-report.json",
    "enrichment-report.json",
    "evidence-report.json",
    "analysis-report.json",
    "recommendation-report.json",
    "run-report.json",
)

#: Per-company artifact directories. `raw/` is deliberately absent.
_ARTIFACT_DIRS = ("evidence", "analyses", "extracted")

#: Patterns that must not appear anywhere in the export. A key cannot reach an artifact -
#: nothing persists a header - but "cannot" is worth checking rather than asserting.
_FORBIDDEN = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]{4,}"),
    re.compile(r"(?i)\b(?:x-api-key|authorization|set-cookie)\b\s*[:=]"),
    re.compile(r"(?i)\b[A-Z_]*API_KEY\b\s*[:=]"),
    re.compile(r"(?<![\w./])/(?:Users|home|root|var/folders)/"),
)

#: A persisted request is bounded before it is exported. The point of the trace is to show
#: the shape of the AI workflow, not to reproduce a whole dossier twice.
_MAX_TRACE_CHARS = 24_000


class ExportError(RuntimeError):
    """The export cannot be produced, or would not be safe to ship."""


@dataclass(slots=True)
class ExportResult:
    """What was written, for the CLI to summarise."""

    root: Path
    files: list[str] = field(default_factory=list)
    trace_company_id: str | None = None
    memos: int = 0
    company_pages: int = 0


def _scrub(text: str, source: str) -> str:
    """Raise if ``text`` carries anything the export must never contain."""
    for pattern in _FORBIDDEN:
        if match := pattern.search(text):
            raise ExportError(
                f"refusing to export {source}: it contains {match.group(0)[:24]!r}, which "
                "looks like a credential, a header or an absolute filesystem path"
            )
    return text


def _copy_text(src: Path, dest: Path, files: list[str], root: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(_scrub(src.read_text(encoding="utf-8"), src.name), encoding="utf-8")
    files.append(dest.relative_to(root).as_posix())


def _select_trace(store: RunStore) -> AnalysisOutcome | None:
    """The highest-ranked candidate with a successful analysis in the current report.

    Ranking order comes from the recommendation report, which is the order the memos and
    the site already present. Deterministic by construction: same artifacts, same choice.
    """
    try:
        recommendation = store.read_recommendation_report()
        analysis = store.read_analysis_report()
    except (StoreError, ValueError):
        return None
    succeeded = {row.company_id: row for row in analysis.candidates if row.succeeded}
    for company_id in recommendation.ordered_company_ids:
        if company_id in succeeded:
            return succeeded[company_id]
    return None


def _trace_payload(path: Path, *, label: str) -> str:
    """One persisted request or response, bounded and checked."""
    payload = read_json(path)
    if isinstance(payload, dict):
        for key in ("user_payload", "payload", "text"):
            value = payload.get(key)
            if isinstance(value, str) and len(value) > _MAX_TRACE_CHARS:
                payload[key] = (
                    value[:_MAX_TRACE_CHARS]
                    + f"\n\n[truncated for the export at {_MAX_TRACE_CHARS:,} characters; "
                    "the full payload is in the run directory]"
                )
    return _scrub(dumps(payload), label)


def _trace_readme(store: RunStore, outcome: AnalysisOutcome) -> str:
    analysis, recommendation = store.read_analysis(outcome.company_id)
    if recommendation is None:
        raise ExportError(f"{outcome.company_id} has an analysis with no recommendation")
    dossier = store.read_evidence(outcome.company_id)
    attempts = outcome.attempts
    last = attempts[-1] if attempts else None
    lines = [
        "# One AI call, end to end",
        "",
        f"This is the analysis request and response for **{outcome.company_id}** - the "
        "highest-ranked candidate in this run with a successful analysis. It is here so "
        "the AI workflow can be read rather than taken on trust.",
        "",
        "## What was sent",
        "",
        f"- provider / model: `{analysis.provider}` / `{analysis.model}`",
        f"- prompt version: `{analysis.prompt_version}` (the system prompt is recorded by "
        "version and hash, not reproduced - it is in `src/vc_scout/prompts/`)",
        f"- thesis version: `{analysis.thesis_version}`",
        "- the user payload is the thesis, the rubric, the source-to-assessment policy and "
        f"this candidate's dossier: {len(dossier.claims)} claim(s), "
        f"{len(dossier.unknowns)} recorded unknown(s), {len(dossier.conflicts)} conflict(s)",
        "- no header is recorded, so no credential can be present",
        "",
        "## What came back, and what happened to it",
        "",
        f"- attempts: {len(attempts)}"
        + (" (the first was rejected and retried)" if len(attempts) > 1 else ""),
        f"- outcome: {'accepted' if outcome.succeeded else 'rejected'}",
        f"- tokens in / out: {last.input_tokens if last else 0} / "
        f"{last.output_tokens if last else 0}",
        "",
        "The response is the model's raw tool call. Everything it says was checked against "
        "this candidate's dossier before anything was written:",
        "",
        "- every cited evidence claim ID and unknown reference exists in that dossier;",
        "- all seven rubric dimensions appear exactly once with the configured maxima;",
        "- every score respects the ceiling its assessment status allows;",
        "- no market-size figure appears that the evidence does not carry;",
        "- exactly two or three recommendation changers are given.",
        "",
        "**The model did not decide the outcome.** The total was recomputed in Python from "
        f"its own per-dimension components ({analysis.total_score}/100), the research "
        f"confidence was computed from coverage ({recommendation.confidence.score:.2f}, "
        f"{recommendation.confidence.level.value}), and the recommendation - "
        f"**{recommendation.decision.value}** - was made by the deterministic policy. The "
        "model's own suggestion was "
        + (
            f"`{analysis.model_suggested_recommendation.value}`"
            if analysis.model_suggested_recommendation
            else "not given"
        )
        + ", recorded for comparison and never consulted.",
        "",
        "## Files",
        "",
        "- `selected-request.json` - exactly what was sent, as persisted by the run",
        "- `selected-response.json` - exactly what came back",
        "- `validation-summary.md` - what was checked, and what the checks decided",
    ]
    return "\n".join(lines) + "\n"


def _validation_summary(store: RunStore, outcome: AnalysisOutcome) -> str:
    analysis, recommendation = store.read_analysis(outcome.company_id)
    if recommendation is None:
        raise ExportError(f"{outcome.company_id} has an analysis with no recommendation")
    rows = [
        "# Validation summary",
        "",
        f"Candidate: **{outcome.company_id}**",
        "",
        "| Attempt | Result | Error category | Validation errors |",
        "| ---: | --- | --- | --- |",
    ]
    for attempt in outcome.attempts:
        errors = "; ".join(attempt.validation_errors[:2]) or "-"
        rows.append(
            f"| {attempt.attempt} | {'accepted' if attempt.succeeded else 'rejected'} | "
            f"{attempt.error_category.value if attempt.error_category else '-'} | {errors} |"
        )
    rows += [
        "",
        "## What the accepted response produced",
        "",
        f"- total score: **{analysis.total_score}/100**, recomputed from the components",
        f"- research confidence: **{recommendation.confidence.level.value}** "
        f"({recommendation.confidence.score:.2f}), computed from coverage",
        f"- recommendation: **{recommendation.decision.value}**, from the deterministic policy",
        f"- guardrails applied: {', '.join(recommendation.guardrails_applied) or 'none'}",
        "- model's advisory suggestion: "
        + (
            analysis.model_suggested_recommendation.value
            if analysis.model_suggested_recommendation
            else "none"
        ),
    ]
    return "\n".join(rows) + "\n"


def _readme(store: RunStore) -> str:
    run = store.read_run_report()
    recommendation = store.read_recommendation_report()
    first = recommendation.ordered_company_ids[0] if recommendation.ordered_company_ids else None
    calls = ", ".join(f"{name} {total:,}" for name, total in sorted(run.token_usage.items()))
    return f"""# VC Scout - run `{run.run_id}`

An AI-augmented investment triage run, exported for review. Everything here was produced by
`vc-scout run` from public sources; nothing in this directory was written by hand.

**Query:** {run.query}
**Provider / model:** `{run.provider}` / `{run.model}`
**Candidates:** {run.candidate_flow.get("source_out", 0)} discovered, \
{recommendation.memos_written} analysed and written up.
**Calls:** {calls or "none recorded"}.

## Preview the site

```bash
python3 -m http.server 8000 --directory demo/site
# then open http://127.0.0.1:8000/
```

The site is static, self-contained and offline: no build step, no framework, no CDN, no
external font or image. Opening `demo/site/index.html` directly works too, except that a
browser's file:// rules may block the stylesheet - the server above avoids that.

## What is here

| Path | What it is |
| --- | --- |
| `site/` | The browsable report: portfolio page plus one page per company |
| `ranking.md` | The same shortlist as Markdown, with the thesis and the thresholds |
| `memos/` | One partner-ready memo per company, ~700-900 words |
| `artifacts/` | Every validated artifact the run produced, in pipeline order |
| `ai-trace/` | One AI call end to end: what was sent, what came back, what was checked |

## Trace one claim back to a source

This is the property the whole pipeline exists to give you. Pick any statement in a memo:

1. Open `memos/{first or "<company>"}.md` and find a sentence carrying a marker like `[S1]`.
2. Scroll to **Sources** at the foot of that memo. `[S1]` resolves to exactly one entry
   there, with the page title, its role, the URL and the date it was read.
3. That URL is public. Open it and check the excerpt quoted beneath the source entry.
4. To go further down: `artifacts/analyses/{first or "<company>"}.json` shows which
   `ev-` evidence claim IDs that statement cited, and
   `artifacts/evidence/{first or "<company>"}.json` shows each of those claims with the
   verbatim excerpt and the `src-` source it came from.

Every marker in a memo resolves to exactly one source, and every source listed is cited
somewhere above it. A statement resting only on a recorded gap is labelled *Open question*
rather than left unattributed.

## What the model did and did not decide

The model extracted evidence with verbatim citations, and wrote the narrative and the
per-dimension scores. It did **not** decide the total - that is recomputed in Python from
its own components - nor the research confidence, which is computed from coverage, nor the
recommendation, which a deterministic policy makes from the score, the confidence and a set
of guardrails. Its own suggestion is recorded next to the binding call so the two can be
compared. `ai-trace/` shows one full round trip of this.

## What is deliberately not here

No raw HTML: `raw/` holds third-party page bodies, which are the input to extraction rather
than evidence. No credentials, headers or absolute filesystem paths - the export refuses to
write if it finds any. The full run directory, including the raw pages and every persisted
request and response, stays under `outputs/runs/{run.run_id}/`.
"""


def export_demo(*, store: RunStore, destination: Path, force: bool = False) -> ExportResult:
    """Assemble ``destination`` from a completed run."""
    if not store.recommendation_report_path().exists():
        raise ExportError(
            f"run {store.run_id!r} has no recommendation-report.json; there is nothing to "
            "export. Run `vc-scout run` first."
        )
    if not (store.site_dir / "index.html").is_file():
        raise ExportError(
            f"run {store.run_id!r} has no generated site; run `vc-scout build-ui` first."
        )
    if destination.exists() and any(destination.iterdir()) and not force:
        raise ExportError(
            f"{destination}/ already exists and is not empty. Pass --force to replace it."
        )

    for name in ("README.md", "ranking.md", "memos", "site", "artifacts", "ai-trace"):
        target = destination / name
        if target.is_dir():
            shutil.rmtree(target)
        elif target.exists():
            target.unlink()
    destination.mkdir(parents=True, exist_ok=True)

    result = ExportResult(root=destination)
    _copy_text(store.ranking_path(), destination / "ranking.md", result.files, destination)

    for memo in sorted(store.resolve("memos").glob("*.md")):
        _copy_text(memo, destination / "memos" / memo.name, result.files, destination)
        result.memos += 1

    for path in sorted(store.site_dir.rglob("*")):
        if path.is_file():
            relative = path.relative_to(store.site_dir)
            _copy_text(path, destination / "site" / relative, result.files, destination)
            if relative.parts[:1] == ("companies",):
                result.company_pages += 1

    for name in _ROOT_ARTIFACTS:
        source = store.resolve(name)
        if source.is_file():
            _copy_text(source, destination / "artifacts" / name, result.files, destination)
    # The UI report is written inside site/, but a reviewer looking for "the reports" reads
    # artifacts/, so it appears in both.
    ui_report = store.site_dir / "ui-report.json"
    if ui_report.is_file():
        _copy_text(
            ui_report, destination / "artifacts" / "ui-report.json", result.files, destination
        )
    for name in _ARTIFACT_DIRS:
        directory = store.resolve(name)
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            _copy_text(
                path, destination / "artifacts" / name / path.name, result.files, destination
            )

    trace = _select_trace(store)
    trace_dir = destination / "ai-trace"
    trace_dir.mkdir(parents=True, exist_ok=True)
    if trace is None:
        (trace_dir / "README.md").write_text(
            "# No AI trace\n\nThis run produced no successful analysis, so there is no call "
            "to show. `artifacts/analysis-report.json` records why each candidate failed.\n",
            encoding="utf-8",
        )
        result.files.append("ai-trace/README.md")
        _write_readme(store, destination, result)
        return result

    attempt = trace.attempts[-1].attempt if trace.attempts else 1
    for label, path in (
        ("selected-request.json", store.analysis_request_path(trace.company_id, attempt=attempt)),
        ("selected-response.json", store.analysis_response_path(trace.company_id, attempt=attempt)),
    ):
        if not path.is_file():
            raise ExportError(f"the selected trace is missing {store.relative(path)}")
        (trace_dir / label).write_text(_trace_payload(path, label=label), encoding="utf-8")
        result.files.append(f"ai-trace/{label}")

    (trace_dir / "README.md").write_text(
        _scrub(_trace_readme(store, trace), "ai-trace/README.md"), encoding="utf-8"
    )
    (trace_dir / "validation-summary.md").write_text(
        _scrub(_validation_summary(store, trace), "ai-trace/validation-summary.md"),
        encoding="utf-8",
    )
    result.files += ["ai-trace/README.md", "ai-trace/validation-summary.md"]
    result.trace_company_id = trace.company_id

    _write_readme(store, destination, result)
    return result


def _write_readme(store: RunStore, destination: Path, result: ExportResult) -> None:
    text = _scrub(_readme(store), "README.md")
    (destination / "README.md").write_text(text, encoding="utf-8")
    result.files.append("README.md")
    result.files.sort()
