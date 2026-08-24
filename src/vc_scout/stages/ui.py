"""Stage 7 - the static research site.

Reads the artifacts the pipeline already validated - the candidate set, the extracted
pages, the evidence dossiers, the analyses with their recommendations, and the
recommendation report that fixed the workflow order - and writes a read-only site of plain
HTML, one stylesheet and one small script.

It never reads a raw page body or an LLM request or response, makes no network call and
selects no provider. Ordering comes from the same comparator the Markdown ranking uses, so
the site and ``ranking.md`` cannot disagree about which company is first.

Failure is per page. A company that cannot be rendered is recorded on ``ui-report.json``,
its stale page is removed, and every other page and the index still build.
"""

from __future__ import annotations

import shutil
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

from vc_scout.models.candidate import Candidate, CandidateSet
from vc_scout.models.page import PageBundle
from vc_scout.models.report import PageFailureRecord, UiReport
from vc_scout.models.source import SourceReference
from vc_scout.render.html import (
    SITE_TEMPLATE_VERSION,
    CompanyView,
    Link,
    build_company_view,
    build_index_view,
    internal_href,
    render_company,
    render_index,
)
from vc_scout.render.ranking import RankingInput, sort_key
from vc_scout.store import RunStore, StoreError

__all__ = ["ASSET_NAMES", "MissingArtifactError", "UiStageOutcome", "run_build_ui"]

#: Copied verbatim beside the generated pages. Not templates - they contain no run data.
ASSET_NAMES = ("styles.css", "app.js")

_ASSET_SOURCE = Path(__file__).resolve().parent.parent / "templates" / "site" / "assets"

#: Everything the generator owns inside ``site/``. ``--force`` replaces exactly these and
#: nothing else, so a site directory that also holds something a human put there is not
#: silently destroyed.
_GENERATED = ("index.html", "ui-report.json")
_GENERATED_DIRS = ("companies", "assets")


class MissingArtifactError(RuntimeError):
    """Raised when an upstream artifact the whole stage depends on is absent."""


@dataclass(slots=True)
class UiStageOutcome:
    """What the stage produced, for the CLI to summarise."""

    report: UiReport
    site_dir: str
    index_path: str
    report_path: str


def _pages(store: RunStore, company_id: str) -> PageBundle | None:
    try:
        return store.read_pages(company_id)
    except StoreError:
        return None


def _load(store: RunStore, candidate: Candidate) -> RankingInput | PageFailureRecord:
    """One candidate's artifacts, or the reason a page cannot be built from them."""
    company_id = candidate.company_id
    try:
        analysis, recommendation = store.read_analysis(company_id)
    except StoreError:
        return PageFailureRecord(
            company_id=company_id,
            reason="no analysis was produced for this candidate",
            detail="see analysis-report.json for the recorded failure",
        )
    if recommendation is None:
        return PageFailureRecord(
            company_id=company_id,
            reason="the analysis carries no recommendation",
            detail="the policy stage did not complete for this candidate",
        )
    try:
        dossier = store.read_evidence(company_id)
    except StoreError:
        return PageFailureRecord(
            company_id=company_id,
            reason="no evidence dossier exists for this candidate",
            detail="a page may not be built from an analysis whose evidence is missing",
        )
    return RankingInput(
        candidate=candidate,
        analysis=analysis,
        recommendation=recommendation,
        dossier=dossier,
        memo_relative_path=f"memos/{company_id}.md",
    )


def _clean(store: RunStore, *, keep: set[str]) -> list[str]:
    """Remove generated files this build did not produce. Returns what was removed."""
    removed: list[str] = []
    site = store.site_dir
    if not site.is_dir():
        return removed
    companies = site / "companies"
    if companies.is_dir():
        for path in sorted(companies.glob("*.html")):
            if path.name not in keep:
                path.unlink()
                removed.append(store.relative(path))
    return removed


def _reset(store: RunStore) -> None:
    """Replace the generated site, and only the generated site.

    Deliberately not ``rmtree(site)``: the directory is a user-visible output path, and a
    build that deletes anything it did not write is a build that can lose work.
    """
    site = store.site_dir
    for name in _GENERATED:
        (site / name).unlink(missing_ok=True)
    for name in _GENERATED_DIRS:
        target = site / name
        if target.is_dir():
            shutil.rmtree(target)


def run_build_ui(*, store: RunStore, force: bool = False) -> UiStageOutcome:
    """Generate the static site from the persisted artifacts of this run."""
    if not store.candidates_path().exists():
        raise MissingArtifactError(
            f"run {store.run_id!r} has no candidates.json; run `vc-scout source` first"
        )
    if not store.analysis_report_path().exists():
        raise MissingArtifactError(
            f"run {store.run_id!r} has no analysis-report.json; run `vc-scout analyze` first"
        )
    if not store.recommendation_report_path().exists():
        raise MissingArtifactError(
            f"run {store.run_id!r} has no recommendation-report.json; run "
            "`vc-scout recommend` first"
        )

    candidate_set: CandidateSet = store.read_candidates()
    candidate_sources: dict[str, SourceReference] = candidate_set.source_index()

    loaded: list[RankingInput] = []
    failures: list[PageFailureRecord] = []
    for candidate in candidate_set.candidates:
        result = _load(store, candidate)
        if isinstance(result, PageFailureRecord):
            failures.append(result)
        else:
            loaded.append(result)

    # The same comparator the Markdown ranking uses, so the two orders cannot drift.
    ordered = sorted(loaded, key=sort_key)
    labels = [
        (item.analysis.company_id, (item.candidate.name if item.candidate else None))
        for item in ordered
    ]

    if force:
        _reset(store)

    views: list[CompanyView] = []
    warnings: list[str] = []
    written: list[str] = []
    for position, item in enumerate(ordered):
        company_id = item.analysis.company_id
        previous = _neighbour(labels, position - 1) if position > 0 else None
        following = _neighbour(labels, position + 1) if position + 1 < len(labels) else None
        try:
            view = build_company_view(
                candidate=item.candidate,
                candidate_sources=candidate_sources,
                pages=_pages(store, company_id),
                dossier=item.dossier,
                analysis=item.analysis,
                recommendation=item.recommendation,
                rank=position + 1,
                previous=previous,
                next_=following,
            )
            path = store.write_text(
                store.site_dir / "companies" / f"{company_id}.html", render_company(view)
            )
        except Exception as exc:  # noqa: BLE001 - one page must never fail the build.
            failures.append(
                PageFailureRecord(
                    company_id=company_id,
                    reason="rendering failed",
                    detail=f"unexpected {type(exc).__name__}",
                )
            )
            continue
        views.append(view)
        written.append(store.relative(path))
        warnings.extend(view.page_warnings)

    statuses: Counter[str] = Counter(
        component.assessment_status.value
        for item in ordered
        for component in item.analysis.score_components
    )
    index_view = build_index_view(
        store.run_id,
        views,
        candidate_count=len(candidate_set.candidates),
        sources_cited=sum(len(view.sources) for view in views),
        status_counts=dict(statuses),
        missing=[
            f"{failure.company_id} - {failure.reason}"
            for failure in sorted(failures, key=lambda f: f.company_id)
        ],
    )
    index_path = store.write_text(store.site_dir / "index.html", render_index(index_view))

    asset_paths: list[str] = []
    for name in ASSET_NAMES:
        target = store.site_dir / "assets" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((_ASSET_SOURCE / name).read_text(encoding="utf-8"), encoding="utf-8")
        asset_paths.append(store.relative(target))

    removed = _clean(store, keep={f"{view.company_id}.html" for view in views})
    report = UiReport(
        run_id=store.run_id,
        template_version=SITE_TEMPLATE_VERSION,
        candidate_count=len(candidate_set.candidates),
        pages_written=len(views) + 1,
        company_pages=[view.company_id for view in views],
        output_paths=[store.relative(index_path), *written, *asset_paths],
        removed_paths=removed,
        recommendations=dict(sorted(Counter(view.decision_slug for view in views).items())),
        confidence_counts=dict(sorted(Counter(view.confidence for view in views).items())),
        component_status_counts=dict(sorted(statuses.items())),
        sources_cited=sum(len(view.sources) for view in views),
        warnings=warnings,
        failures=sorted(failures, key=lambda failure: failure.company_id),
    )
    report_path = store.write_model(store.site_dir / "ui-report.json", report)
    return UiStageOutcome(
        report=report,
        site_dir=store.relative(store.site_dir),
        index_path=store.relative(index_path),
        report_path=store.relative(report_path),
    )


def _neighbour(labels: list[tuple[str, str | None]], position: int) -> Link:
    """A previous/next link, with the company's own name as its label."""
    company_id, name = labels[position]
    return Link(label=name or company_id, href=internal_href(f"{company_id}.html"))
