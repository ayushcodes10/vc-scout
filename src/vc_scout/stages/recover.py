"""Targeted repair of the candidates an analysis run failed on.

A live run left thirteen of fifteen analyses good and two rejected on shape. Re-running all
fifteen would spend twenty-six requests to fix two, and would discard thirteen analyses a
partner could already read. `--company-id` is not the answer either: it writes a
`filtered_to` report describing one candidate, which cannot stand in for a full-run report
without claiming the other fourteen were never analysed.

So this stage does the narrow thing. It reads the full report, retries only the failed
candidates through the *same* single-candidate path the run uses, and merges the results
back into the full report - preserving candidate order, preserving every successful
analysis byte for byte, and recomputing every aggregate from the merged outcomes rather
than adjusting the old ones.

Two rules keep it honest:

* **The attempt history grows, it does not get replaced.** Recovery attempts are appended
  with a ``recovery_round``, so the report still shows what the first pass cost.
* **A report never claims a success it cannot show.** The merged report is written from the
  outcomes, and the analysis file for every succeeded outcome is read back before the
  downstream stages are allowed to run.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime

from vc_scout.fingerprint import evidence_fingerprint
from vc_scout.llm.provider import LlmProvider, ModelConfig
from vc_scout.models.analysis import StartupAnalysis
from vc_scout.models.candidate import Candidate
from vc_scout.models.report import AnalysisOutcome, AnalysisReport
from vc_scout.stages.analysis import (
    ANALYSIS_PROMPT_VERSION,
    MAX_ATTEMPTS,
    aggregate_outcomes,
    analyse_candidate,
    outcome_for,
)
from vc_scout.store import RunStore, StoreError

__all__ = ["RecoveryError", "RecoveryOutcome", "recover_analyses"]


class RecoveryError(RuntimeError):
    """Recovery cannot run, or cannot run safely, against this report."""


@dataclass(slots=True)
class RecoveryOutcome:
    """What the recovery attempted and what it produced."""

    report: AnalysisReport
    report_path: str
    attempted: list[str] = field(default_factory=list)
    recovered: list[str] = field(default_factory=list)
    still_failed: list[str] = field(default_factory=list)
    analyses: list[StartupAnalysis] = field(default_factory=list)
    recovery_round: int = 1
    #: True when the merged report and the analyses on disk agree about every success.
    verified: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.recovered)


def _next_round(report: AnalysisReport) -> int:
    """One past the highest recovery round any attempt in the report records."""
    rounds = [
        attempt.recovery_round for outcome in report.candidates for attempt in outcome.attempts
    ]
    return max(rounds, default=0) + 1


def failed_company_ids(report: AnalysisReport) -> list[str]:
    """Candidates the report records as failed, in report order."""
    return [outcome.company_id for outcome in report.candidates if not outcome.succeeded]


def plan_recovery(report: AnalysisReport, *, only: list[str] | None = None) -> list[str]:
    """The candidates to retry, or a refusal. Nothing is called until this returns.

    A filtered report is refused outright: it describes one candidate, so merging into it
    would produce a document that looks like a full run and is not one.
    """
    if report.filtered_to is not None:
        raise RecoveryError(
            f"analysis-report.json is filtered to {report.filtered_to!r}, so it is not a "
            "full-run report and cannot be repaired into one. Re-run `vc-scout analyze` "
            "without --company-id first."
        )
    failed = failed_company_ids(report)
    if only is None or not only:
        return failed

    known = {outcome.company_id for outcome in report.candidates}
    requested: list[str] = []
    for company_id in only:
        if company_id not in known:
            raise RecoveryError(
                f"run has no candidate {company_id!r}. Known candidates: {', '.join(sorted(known))}"
            )
        if company_id not in failed:
            raise RecoveryError(
                f"{company_id!r} is recorded as succeeded, and recovery only retries "
                "failures. Use `vc-scout analyze --company-id` to deliberately replace a "
                "successful analysis."
            )
        if company_id not in requested:
            requested.append(company_id)
    return [company_id for company_id in failed if company_id in requested]


def _candidate(store: RunStore, company_id: str) -> Candidate:
    for candidate in store.read_candidates().candidates:
        if candidate.company_id == company_id:
            return candidate
    raise RecoveryError(f"candidates.json has no candidate {company_id!r}")


def _merge(
    original: AnalysisOutcome, fresh: AnalysisOutcome, *, recovery_round: int
) -> AnalysisOutcome:
    """The recovery result, carrying the original attempt history in front of its own."""
    history = list(original.attempts) + [
        attempt.model_copy(update={"recovery_round": recovery_round}) for attempt in fresh.attempts
    ]
    return fresh.model_copy(update={"attempts": history})


def recover_analyses(
    *,
    store: RunStore,
    provider: LlmProvider,
    config: ModelConfig,
    only: list[str] | None = None,
    now: datetime | None = None,
) -> RecoveryOutcome:
    """Retry the failed candidates and merge the results into the full report."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    try:
        report = store.read_analysis_report()
    except (StoreError, ValueError) as exc:
        raise RecoveryError(
            f"run {store.run_id!r} has no readable analysis-report.json; run "
            "`vc-scout analyze` first"
        ) from exc

    targets = plan_recovery(report, only=only)
    recovery_round = _next_round(report)
    if not targets:
        return RecoveryOutcome(
            report=report,
            report_path=store.relative(store.analysis_report_path()),
            recovery_round=recovery_round,
            verified=_verify(store, report),
        )

    # Everything below this line may spend a request, so every refusal is above it.
    candidates = {company_id: _candidate(store, company_id) for company_id in targets}
    results: dict[str, AnalysisOutcome] = {}
    analyses: list[StartupAnalysis] = []
    extras: Counter[str] = Counter()

    for company_id in targets:
        candidate = candidates[company_id]
        # Only this candidate's attempt files are cleared, so the recovery's own artifacts
        # are exactly what remains for it and no other candidate is touched.
        extras["stale_attempts_removed"] += store.delete_llm_attempts(company_id, stage="analysis")
        try:
            dossier = store.read_evidence(company_id)
        except StoreError:
            continue
        run = analyse_candidate(
            candidate, dossier, store=store, provider=provider, config=config, now=now
        )
        fresh = outcome_for(company_id, run, dossier)
        if run.analysis is not None and run.recommendation is not None:
            store.write_analysis(run.analysis, run.recommendation)
            analyses.append(run.analysis)
        elif store.delete_analysis(company_id):
            extras["stale_analyses_removed"] += 1
        results[company_id] = fresh
        if run.abort_run:
            # The provider failed at run level: every remaining retry would fail the same
            # way, so none is sent.
            extras["run_aborted"] += 1
            break

    merged = [
        _merge(outcome, results[outcome.company_id], recovery_round=recovery_round)
        if outcome.company_id in results
        else outcome
        for outcome in report.candidates
    ]
    aggregates = aggregate_outcomes(merged, extras=dict(extras))
    recovered = [company_id for company_id, outcome in results.items() if outcome.succeeded]
    still_failed = [outcome.company_id for outcome in merged if not outcome.succeeded]

    updated = report.model_copy(
        update={
            "generated_at": now,
            "prompt_version": ANALYSIS_PROMPT_VERSION,
            "provider": provider.name,
            "model": config.model,
            "candidates": merged,
            "counts": aggregates.counts,
            "recommendations": aggregates.recommendations,
            "guardrails": aggregates.guardrails,
            "failures_by_category": aggregates.failures_by_category,
            "filtered_to": None,
            "limits": {**report.limits, "max_attempts": MAX_ATTEMPTS},
            "upstream_fingerprint": evidence_fingerprint(
                [store.read_evidence(cid) for cid in store.evidence_company_ids()]
            ),
            "notes": _notes(report, targets, recovered, recovery_round),
        }
    )
    verified = _verify(store, updated)
    path = store.write_analysis_report(updated)
    return RecoveryOutcome(
        report=updated,
        report_path=store.relative(path),
        attempted=list(targets),
        recovered=recovered,
        still_failed=still_failed,
        analyses=analyses,
        recovery_round=recovery_round,
        verified=verified,
    )


def _verify(store: RunStore, report: AnalysisReport) -> bool:
    """Every outcome the report calls succeeded has an analysis file that loads.

    A report claiming fifteen successes while fourteen files exist would send the memo and
    the site stage looking for something that is not there, and the gap would surface as a
    render failure rather than as the report being wrong.
    """
    for outcome in report.candidates:
        if not outcome.succeeded:
            continue
        try:
            analysis, recommendation = store.read_analysis(outcome.company_id)
        except (StoreError, ValueError):
            return False
        if recommendation is None or analysis.company_id != outcome.company_id:
            return False
    return True


def _notes(
    report: AnalysisReport, targets: list[str], recovered: list[str], recovery_round: int
) -> list[str]:
    """The original notes, plus one line recording what this recovery did."""
    kept = [note for note in report.notes if not note.startswith("Recovery round ")]
    return [
        *kept,
        f"Recovery round {recovery_round}: retried {len(targets)} failed candidate(s) "
        f"({', '.join(targets)}) and recovered {len(recovered)}. Every other analysis was "
        "left exactly as it was, and the totals above are recomputed from the merged "
        "outcomes. Attempts from earlier rounds are retained with their round number.",
    ]
