"""Domain models - the artifact contract between pipeline stages.

See ``docs/PLAN.md`` section 3 for the on-disk layout these models serialise into.
"""

from vc_scout.models.analysis import (
    AnalysisSection,
    RiskItem,
    ScoreComponent,
    StartupAnalysis,
)
from vc_scout.models.candidate import Candidate, CandidateSet
from vc_scout.models.enums import (
    ClaimLabel,
    ComponentStatus,
    ConfidenceLevel,
    Recommendation,
    RubricDimension,
    SourceKind,
    StageName,
    StageStatus,
    TractionKind,
)
from vc_scout.models.evidence import EvidenceClaim, EvidenceDossier
from vc_scout.models.manifest import CompanyOutcome, RunManifest, StageRecord
from vc_scout.models.page import ExtractedPage, PageBundle
from vc_scout.models.recommendation import RecommendationResult, ResearchConfidence
from vc_scout.models.source import SourceReference, TractionSignal, is_safe_url

__all__ = [
    "AnalysisSection",
    "Candidate",
    "CandidateSet",
    "ClaimLabel",
    "CompanyOutcome",
    "ComponentStatus",
    "ConfidenceLevel",
    "EvidenceClaim",
    "EvidenceDossier",
    "ExtractedPage",
    "PageBundle",
    "Recommendation",
    "RecommendationResult",
    "ResearchConfidence",
    "RiskItem",
    "RubricDimension",
    "RunManifest",
    "ScoreComponent",
    "SourceKind",
    "SourceReference",
    "StageName",
    "StageRecord",
    "StageStatus",
    "StartupAnalysis",
    "TractionKind",
    "TractionSignal",
    "is_safe_url",
]
