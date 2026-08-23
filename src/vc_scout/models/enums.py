"""Closed vocabularies shared by every artifact.

These are string enums so that persisted JSON stays human-readable and reviewable.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "ClaimLabel",
    "ComponentStatus",
    "ConfidenceLevel",
    "Recommendation",
    "RubricDimension",
    "SourceKind",
    "StageName",
    "StageStatus",
    "TractionKind",
]


class RubricDimension(StrEnum):
    """The seven scored dimensions. Weights live in :mod:`vc_scout.rubric`."""

    PAIN_ROI = "pain_roi"
    WEDGE = "wedge"
    DISTRIBUTION = "distribution"
    DEFENSIBILITY = "defensibility"
    TEAM = "team"
    TRACTION = "traction"
    MARKET_TIMING = "market_timing"


class SourceKind(StrEnum):
    HN_STORY = "hn_story"
    HN_COMMENTS = "hn_comments"
    COMPANY_PAGE = "company_page"
    OTHER = "other"


class ClaimLabel(StrEnum):
    """Provenance of a claim. Mislabelling here is the failure mode this pipeline
    exists to prevent, so the vocabulary is deliberately small."""

    COMPANY_CLAIM = "company_claim"
    THIRD_PARTY = "third_party"
    INFERENCE = "inference"


class ComponentStatus(StrEnum):
    SCORED = "scored"
    #: No supporting evidence was found. Explicitly not the same as scoring zero on merit.
    UNKNOWN = "unknown"


class ConfidenceLevel(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class Recommendation(StrEnum):
    PASS = "pass"  # noqa: S105 - an investment decision, not a credential
    WATCH = "watch"
    TAKE_A_MEETING = "take_a_meeting"


class TractionKind(StrEnum):
    HN_POINTS = "hn_points"
    HN_COMMENTS = "hn_comments"
    CUSTOMER_REFERENCE = "customer_reference"
    FUNDING_MENTION = "funding_mention"
    HEADCOUNT = "headcount"
    LAUNCH_DATE = "launch_date"
    PRICING_PUBLISHED = "pricing_published"
    INTEGRATION_LISTED = "integration_listed"
    OTHER = "other"


class StageName(StrEnum):
    SOURCE = "source"
    ENRICH = "enrich"
    EXTRACT = "extract"
    ANALYZE = "analyze"
    POLICY = "policy"
    RENDER = "render"
    BUILD_SITE = "build_site"


class StageStatus(StrEnum):
    PENDING = "pending"
    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"
    SKIPPED = "skipped"
