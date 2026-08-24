"""Closed vocabularies shared by every artifact.

These are string enums so that persisted JSON stays human-readable and reviewable.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "AssessmentStatus",
    "ClaimLabel",
    "EvidenceCategory",
    "ComponentStatus",
    "ConfidenceLevel",
    "InferenceStatus",
    "LlmErrorCategory",
    "EnrichmentStatus",
    "FetchFailure",
    "PageRole",
    "Recommendation",
    "ThesisFit",
    "RelevanceClass",
    "RubricDimension",
    "SourceKind",
    "VerificationStatus",
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


class AssessmentStatus(StrEnum):
    """How well the supplied evidence supports a scored dimension.

    This is a statement about the *evidence*, not about the company. ``not_assessable``
    means nothing was found, which is not the same as finding something bad - that is
    ``contradicted``.
    """

    SUPPORTED = "supported"
    PARTIALLY_SUPPORTED = "partially_supported"
    CONTRADICTED = "contradicted"
    NOT_ASSESSABLE = "not_assessable"


class ThesisFit(StrEnum):
    """Whether the evidence places the company inside the firm's thesis."""

    ALIGNED = "aligned"
    ADJACENT = "adjacent"
    #: The evidence positively shows the company is outside the thesis - developer
    #: infrastructure, a personal project, an enterprise platform. Not the same as
    #: an absence of evidence.
    MISMATCH = "mismatch"
    UNDETERMINED = "undetermined"


class EvidenceCategory(StrEnum):
    """What an evidence claim is about. Deliberately coarser than the scoring rubric:
    extraction should not be shaped by how the claim will later be scored."""

    TEAM = "team"
    PRODUCT = "product"
    MARKET = "market"
    TRACTION = "traction"
    RISK = "risk"


class VerificationStatus(StrEnum):
    """How well-attested a claim is.

    The distinction that matters most: a company saying something about itself is not the
    same as a third party saying it. ``independently_supported`` is reserved for claims
    backed by separate eligible sources and is validated, never taken on trust.
    """

    #: Stated by the company on its own pages. Marketing until proven otherwise.
    COMPANY_CLAIM = "company_claim"
    #: Hacker News points, comments and launch timestamps. Reaction, not verification.
    COMMUNITY_SIGNAL = "community_signal"
    #: Supported by two or more separate eligible sources.
    INDEPENDENTLY_SUPPORTED = "independently_supported"


class InferenceStatus(StrEnum):
    """Whether a claim is stated in the sources or reasoned from them."""

    EXPLICIT = "explicit"
    INFERRED = "inferred"


class LlmErrorCategory(StrEnum):
    """Why an evidence extraction attempt failed. Recorded per attempt and per candidate."""

    MISSING_API_KEY = "missing_api_key"
    MISSING_EVIDENCE = "missing_evidence"
    PROVIDER_TIMEOUT = "provider_timeout"
    PROVIDER_HTTP_ERROR = "provider_http_error"
    PROVIDER_RATE_LIMITED = "provider_rate_limited"
    MALFORMED_RESPONSE = "malformed_response"
    SCHEMA_VALIDATION_FAILED = "schema_validation_failed"
    UNKNOWN_SOURCE_REFERENCE = "unknown_source_reference"
    UNKNOWN_EVIDENCE_REFERENCE = "unknown_evidence_reference"
    INVALID_SCORE = "invalid_score"
    INVALID_RECOMMENDATION = "invalid_recommendation"
    EXCERPT_NOT_FOUND = "excerpt_not_found"
    PERMANENT_FAILURE = "permanent_failure"


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
