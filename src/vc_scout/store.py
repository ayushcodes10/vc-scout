"""Run-scoped artifact storage.

Every path under ``outputs/runs/<run-id>/`` is constructed here and nowhere else. All
identifiers used as path segments are validated against a strict pattern and every
resolved path is asserted to live inside the run directory, so a hostile company name or
run ID cannot escape it.
"""

from __future__ import annotations

from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from vc_scout.models.analysis import StartupAnalysis
from vc_scout.models.candidate import CandidateSet
from vc_scout.models.evidence import EvidenceDossier
from vc_scout.models.manifest import RunManifest
from vc_scout.models.page import PageBundle
from vc_scout.models.recommendation import RecommendationResult
from vc_scout.models.report import SourceReport
from vc_scout.util.ids import is_valid_company_id, is_valid_run_id, slugify
from vc_scout.util.jsonio import read_json, write_json

__all__ = ["DEFAULT_RUNS_ROOT", "RunStore", "StoreError"]

DEFAULT_RUNS_ROOT = Path("outputs/runs")

ModelT = TypeVar("ModelT", bound=BaseModel)


class StoreError(RuntimeError):
    """Raised when an artifact path or identifier is unusable."""


class RunStore:
    """Reads and writes the artifacts of a single run."""

    def __init__(self, run_id: str, runs_root: Path = DEFAULT_RUNS_ROOT) -> None:
        if not is_valid_run_id(run_id):
            raise StoreError(
                f"invalid run id {run_id!r}; expected lowercase alphanumerics, dots, "
                "dashes or underscores"
            )
        self.run_id = run_id
        self.runs_root = Path(runs_root)
        self.root = (self.runs_root / run_id).resolve()

    # -- path construction -------------------------------------------------

    def resolve(self, *parts: str) -> Path:
        """Resolve a path inside the run directory, refusing to escape it."""
        candidate = self.root.joinpath(*parts)
        # resolve() on a non-existent path still normalises ".." segments.
        resolved = Path(candidate).resolve()
        if resolved != self.root and self.root not in resolved.parents:
            raise StoreError(f"path {'/'.join(parts)!r} escapes the run directory")
        return resolved

    def _company_path(self, *parts: str, company_id: str, suffix: str) -> Path:
        if not is_valid_company_id(company_id):
            raise StoreError(f"invalid company id {company_id!r}")
        return self.resolve(*parts, f"{company_id}{suffix}")

    @property
    def raw_dir(self) -> Path:
        return self.resolve("raw")

    @property
    def site_dir(self) -> Path:
        return self.resolve("site")

    def candidates_path(self) -> Path:
        return self.resolve("candidates.json")

    def source_report_path(self) -> Path:
        return self.resolve("source-report.json")

    def raw_hn_path(self, variant_label: str, *, page: int = 0) -> Path:
        """Path for a verbatim Algolia response.

        The label is slugified because it becomes a filename; an unusable label is an
        error rather than something to silently rewrite.
        """
        slug = slugify(variant_label)
        if not slug:
            raise StoreError(f"unusable raw response label {variant_label!r}")
        return self.resolve("raw", "hn", f"{slug}-p{page}.json")

    def extracted_path(self, company_id: str) -> Path:
        return self._company_path("extracted", company_id=company_id, suffix=".json")

    def evidence_path(self, company_id: str) -> Path:
        return self._company_path("evidence", company_id=company_id, suffix=".json")

    def analysis_path(self, company_id: str) -> Path:
        return self._company_path("analyses", company_id=company_id, suffix=".json")

    def memo_path(self, company_id: str) -> Path:
        return self._company_path("memos", company_id=company_id, suffix=".md")

    def ranking_path(self) -> Path:
        return self.resolve("ranking.md")

    def manifest_path(self) -> Path:
        return self.resolve("run-manifest.json")

    def relative(self, path: Path) -> str:
        """Path relative to the run root, for recording in the manifest.

        Manifests must never contain absolute filesystem paths.
        """
        return path.resolve().relative_to(self.root).as_posix()

    # -- generic IO --------------------------------------------------------

    def ensure_root(self) -> Path:
        self.root.mkdir(parents=True, exist_ok=True)
        return self.root

    def write_model(self, path: Path, model: BaseModel) -> Path:
        write_json(path, model.model_dump(mode="json"))
        return path

    def read_model(self, path: Path, model_type: type[ModelT]) -> ModelT:
        if not path.exists():
            raise StoreError(f"missing artifact {self.relative(path)!r} in run {self.run_id!r}")
        return model_type.model_validate(read_json(path))

    def write_text(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    # -- typed accessors ---------------------------------------------------

    def write_candidates(self, candidates: CandidateSet) -> Path:
        return self.write_model(self.candidates_path(), candidates)

    def read_candidates(self) -> CandidateSet:
        return self.read_model(self.candidates_path(), CandidateSet)

    def write_source_report(self, report: SourceReport) -> Path:
        return self.write_model(self.source_report_path(), report)

    def read_source_report(self) -> SourceReport:
        return self.read_model(self.source_report_path(), SourceReport)

    def write_pages(self, bundle: PageBundle) -> Path:
        return self.write_model(self.extracted_path(bundle.company_id), bundle)

    def read_pages(self, company_id: str) -> PageBundle:
        return self.read_model(self.extracted_path(company_id), PageBundle)

    def write_evidence(self, dossier: EvidenceDossier) -> Path:
        return self.write_model(self.evidence_path(dossier.company_id), dossier)

    def read_evidence(self, company_id: str) -> EvidenceDossier:
        return self.read_model(self.evidence_path(company_id), EvidenceDossier)

    def write_analysis(
        self, analysis: StartupAnalysis, recommendation: RecommendationResult | None = None
    ) -> Path:
        """Persist an analysis and, once the policy has run, its recommendation.

        Both live in one document because the required artifact layout has no ``policy/``
        directory. They stay separate top-level keys so the stage boundary remains visible.
        """
        payload: dict[str, object] = {"analysis": analysis.model_dump(mode="json")}
        if recommendation is not None:
            payload["recommendation"] = recommendation.model_dump(mode="json")
        path = self.analysis_path(analysis.company_id)
        write_json(path, payload)
        return path

    def read_analysis(self, company_id: str) -> tuple[StartupAnalysis, RecommendationResult | None]:
        path = self.analysis_path(company_id)
        if not path.exists():
            raise StoreError(f"missing analysis for {company_id!r} in run {self.run_id!r}")
        payload = read_json(path)
        analysis = StartupAnalysis.model_validate(payload["analysis"])
        raw_recommendation = payload.get("recommendation")
        recommendation = (
            RecommendationResult.model_validate(raw_recommendation)
            if raw_recommendation is not None
            else None
        )
        return analysis, recommendation

    def write_manifest(self, manifest: RunManifest) -> Path:
        return self.write_model(self.manifest_path(), manifest)

    def read_manifest(self) -> RunManifest:
        return self.read_model(self.manifest_path(), RunManifest)

    # -- discovery ---------------------------------------------------------

    def company_ids(self) -> list[str]:
        """Company IDs that have a persisted analysis, in stable order."""
        directory = self.resolve("analyses")
        if not directory.is_dir():
            return []
        return sorted(path.stem for path in directory.glob("*.json"))

    def exists(self) -> bool:
        return self.root.is_dir()
