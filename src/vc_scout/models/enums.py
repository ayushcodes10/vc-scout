"""Closed vocabularies shared by every artifact.

These are string enums so that persisted JSON stays human-readable and reviewable.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "ClaimLabel",
    "ComponentStatus",
    "ConfidenceLevel",
    "EnrichmentStatus",
    "FetchFailure",
    "PageRole",
    "Recommendation",
    "RelevanceClass",
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


class RelevanceClass(StrEnum):
    """How well a discovered story matches the topic being searched for.

    Assigned before any page is fetched, from the story's own text alone. This is a
    topical judgment for spending enrichment budget, not a judgment about the company.
    """

    #: An AI-automation signal together with a business buyer or an operational workflow.
    DIRECT = "direct"
    #: An AI-automation signal with no identifiable buyer or workflow.
    ADJACENT = "adjacent"
    #: No meaningful AI-automation signal.
    IRRELEVANT = "irrelevant"


class PageRole(StrEnum):
    """Why a page was fetched. Ordering here is the crawl priority within a candidate."""

    HOMEPAGE = "homepage"
    #: The exact URL posted to Hacker News, when it differs from the site origin.
    LAUNCH = "launch"
    PRODUCT = "product"
    PRICING = "pricing"
    CUSTOMERS = "customers"
    ABOUT = "about"
    TEAM = "team"
    CHANGELOG = "changelog"
    BLOG = "blog"


class FetchFailure(StrEnum):
    """Why one page could not be turned into readable text.

    Recorded per page and rolled up per candidate. A failure is never fatal: a company with
    an unreachable site stays in the run with zero pages, so the gap is visible downstream
    rather than silently removing the company.
    """

    UNSAFE_URL = "unsafe_url"
    TIMEOUT = "timeout"
    CONNECTION_ERROR = "connection_error"
    HTTP_ERROR = "http_error"
    BLOCKED = "blocked"
    TOO_MANY_REDIRECTS = "too_many_redirects"
    NON_HTML = "non_html"
    OVERSIZED = "oversized_response"
    ROBOTS_DISALLOWED = "robots_disallowed"
    EXTRACTION_FAILED = "extraction_failed"
    NO_WEBSITE = "no_website_recorded"


class EnrichmentStatus(StrEnum):
    """Per-candidate outcome of the enrich stage."""

    #: Every page attempted was retrieved and extracted.
    SUCCESS = "success"
    #: At least one page succeeded and at least one failed.
    PARTIAL = "partial"
    #: No page could be retrieved. The candidate stays in the run regardless.
    FAILED = "failed"


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
