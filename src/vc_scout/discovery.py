"""Discovery rules: what counts as a candidate, how relevant it is, and how it ranks.

Three things live here, all deliberately separate from investment scoring:

* **URL acceptance** - whether a Hacker News link points at something that could be a
  company's own product surface.
* **Relevance** - a deterministic, intent-aware classification of how well a story matches
  the topic being searched for, using three concept groups.
* **Discovery rank** - a lexicographic ordering used only to decide which candidates are
  worth spending enrichment budget on.

None of it is an investment score. It is all computed before any page is fetched, knows
nothing about the thesis rubric, and never reaches :mod:`vc_scout.policy`.
"""

from __future__ import annotations

import math
import re
from urllib.parse import urlsplit

from vc_scout.models.discovery import DISCOVERY_FORMULA_VERSION, DiscoveryRank
from vc_scout.models.enums import RelevanceClass
from vc_scout.models.source import is_safe_url
from vc_scout.util.ids import normalize_url, registrable_domain

__all__ = [
    "AI_AUTOMATION_TERMS",
    "BLOCKED_DOMAINS",
    "BUSINESS_BUYER_TERMS",
    "DISCOVERY_FORMULA_VERSION",
    "INCUMBENT_DOMAINS",
    "MIN_RELEVANCE_SCORE",
    "OPERATIONAL_WORKFLOW_TERMS",
    "DiscoveryRank",
    "RejectionReason",
    "RelevanceClass",
    "accept_product_url",
    "canonical_website",
    "classify_relevance",
    "discovery_rank",
    "engagement_score",
    "parse_story_title",
    "quality_score",
    "recency_score",
]

# --------------------------------------------------------------------------
# URL acceptance
# --------------------------------------------------------------------------

#: Domains that are never a company's own product surface. Discussion boards, code hosts,
#: publishers, package registries and document hosts.
BLOCKED_DOMAINS = frozenset(
    {
        "amazon.com",
        "apps.apple.com",
        "arxiv.org",
        "bitbucket.org",
        "blogspot.com",
        "codeberg.org",
        "dev.to",
        "docs.google.com",
        "dropbox.com",
        "facebook.com",
        "figma.com",
        "gist.github.com",
        "github.com",
        "github.io",
        "gitlab.com",
        "hackernoon.com",
        "hn.algolia.com",
        "huggingface.co",
        "instagram.com",
        "linkedin.com",
        "medium.com",
        "news.ycombinator.com",
        "npmjs.com",
        "play.google.com",
        "producthunt.com",
        "pypi.org",
        "reddit.com",
        "sites.google.com",
        "substack.com",
        "techcrunch.com",
        "twitter.com",
        "vimeo.com",
        "wikipedia.org",
        "x.com",
        "ycombinator.com",
        "youtu.be",
        "youtube.com",
    }
)

#: Companies that are emphatically not seed stage. Their launch posts match a thesis query
#: as well as any startup's, and a seed fund cannot invest in any of them.
INCUMBENT_DOMAINS = frozenset(
    {
        "adobe.com",
        "anthropic.com",
        "apple.com",
        "aws.amazon.com",
        "blog.google",
        "cloudflare.com",
        "deepmind.google",
        "google.com",
        "ibm.com",
        "meta.com",
        "microsoft.com",
        "netflix.com",
        "nvidia.com",
        "openai.com",
        "oracle.com",
        "salesforce.com",
        "shopify.com",
        "stripe.com",
        "uber.com",
    }
)


class RejectionReason:
    """Stable identifiers for why a hit did not become a candidate.

    Counted in the sourcing report, so a partner can see what the funnel discarded rather
    than only what survived it.
    """

    MALFORMED = "malformed_hit"
    NO_URL = "no_external_url"
    UNSAFE_URL = "unsafe_url"
    BLOCKED_DOMAIN = "blocked_domain"
    INCUMBENT = "incumbent_domain"
    DUPLICATE_STORY = "duplicate_story"
    DUPLICATE_DOMAIN = "duplicate_domain"
    IRRELEVANT = "irrelevant_topic"
    BELOW_THRESHOLD = "below_relevance_threshold"
    ADJACENT_SHARE = "adjacent_share_reached"
    OVER_LIMIT = "below_limit_cutoff"


def _matches(domain: str, blocklist: frozenset[str]) -> bool:
    """Exact match, or a subdomain of a blocked host such as someone.github.io."""
    return domain in blocklist or any(domain.endswith(f".{entry}") for entry in blocklist)


def accept_product_url(url: str) -> tuple[bool, str | None]:
    """Decide whether ``url`` may represent a company's own product surface.

    Returns ``(accepted, rejection_reason)``.
    """
    if not url or not url.strip():
        return False, RejectionReason.NO_URL
    if not is_safe_url(url):
        return False, RejectionReason.UNSAFE_URL

    domain = registrable_domain(normalize_url(url))
    if not domain:
        return False, RejectionReason.UNSAFE_URL
    if _matches(domain, BLOCKED_DOMAINS):
        return False, RejectionReason.BLOCKED_DOMAIN
    if _matches(domain, INCUMBENT_DOMAINS):
        return False, RejectionReason.INCUMBENT
    return True, None


def canonical_website(url: str) -> tuple[str, str | None]:
    """Company website to enrich, plus a note when it differs from the launch URL.

    A Show HN link often points at a blog post, a research page or a deep product route.
    Enrichment wants the company's own front door, so a URL carrying a path or query is
    reduced to its origin. The host is never rewritten - only the path is dropped - so this
    can never turn a link into a *different* company's website.

    Returns ``(website, note)``; ``note`` is ``None`` when nothing was changed.
    """
    canonical = normalize_url(url)
    split = urlsplit(canonical)
    if split.path in ("", "/") and not split.query:
        return canonical, None
    origin = f"{split.scheme}://{split.netloc}/"
    return origin, (
        f"Launch URL {canonical} points to a subpath; website normalised to the site "
        f"origin {origin} for enrichment. The launch URL is preserved as a source."
    )


# --------------------------------------------------------------------------
# Relevance
# --------------------------------------------------------------------------

#: Group A - does this look like an AI-automation product at all?
AI_AUTOMATION_TERMS = frozenset(
    {
        "ai",
        "agent",
        "agents",
        "agentic",
        "automation",
        "automated",
        "automate",
        "copilot",
        "assistant",
    }
)

#: Group B - is there an identifiable business buyer?
#:
#: "store" is deliberately absent. It reads as a retail buyer but collides with
#: "vector store" and "embeddings store", which misclassified generic AI infrastructure
#: as a retail workflow product. Retail buyers are covered by "retailer" and by the
#: retail and ecommerce terms in group C.
BUSINESS_BUYER_TERMS = frozenset(
    {
        "smb",
        "small business",
        "small businesses",
        "business",
        "businesses",
        "company",
        "companies",
        "team",
        "teams",
        "merchant",
        "merchants",
        "retailer",
        "retailers",
        "sme",
        "franchise",
        "contractor",
        "contractors",
        "clinic",
        "clinics",
        "practice",
        "practices",
        "restaurant",
        "restaurants",
        "storefront",
    }
)

#: Group C - is there an identifiable operational workflow?
OPERATIONAL_WORKFLOW_TERMS = frozenset(
    {
        "operations",
        "ops",
        "workflow",
        "workflows",
        "support",
        "customer service",
        "customer support",
        "helpdesk",
        "sales",
        "marketing",
        "lead",
        "leads",
        "crm",
        "finance",
        "accounting",
        "bookkeeping",
        "invoice",
        "invoices",
        "invoicing",
        "billing",
        "payroll",
        "scheduling",
        "schedule",
        "booking",
        "bookings",
        "dispatch",
        "back office",
        "backoffice",
        "ecommerce",
        "retail",
        "inventory",
        "fulfilment",
        "fulfillment",
        "recruiting",
        "hiring",
        "hr",
        "onboarding",
        "procurement",
        "quoting",
        "quotes",
        "compliance",
        "reconciliation",
    }
)

#: Weights of the three relevance groups. They sum to 1.0. The buyer and workflow groups
#: together outweigh the AI group, so a story naming only "AI agents" cannot score as high
#: as one that also names who it is for or what it does.
_W_AI = 0.40
_W_BUYER = 0.25
_W_WORKFLOW = 0.35
assert abs(_W_AI + _W_BUYER + _W_WORKFLOW - 1.0) < 1e-9

#: Matches beyond this per group stop adding score, so keyword stuffing gains nothing.
_GROUP_SATURATION = 2

#: A candidate below this is discarded before ranking, whatever its class.
MIN_RELEVANCE_SCORE = 0.20


def _word_blob(*texts: str) -> str:
    """Whitespace-delimited, punctuation-stripped text, padded for whole-word matching."""
    joined = " ".join(text for text in texts if text)
    return " " + re.sub(r"[^a-z0-9]+", " ", joined.lower()).strip() + " "


def _host_blob(url: str) -> str:
    """Host labels except the TLD, concatenated.

    The final label is dropped because a ``.ai`` domain is a fact about domain fashion, not
    about the product. Labels are concatenated because company hosts run words together
    ("useinvoiceflow"), so this blob is matched by substring rather than by word.
    """
    host = urlsplit(url).hostname or ""
    labels = host.split(".")
    if len(labels) > 1:
        labels = labels[:-1]
    if labels and labels[0] == "www":
        labels = labels[1:]
    return "".join(labels)


def _term_matches(term: str, words: str, host: str) -> bool:
    """Whether one concept term appears in the searchable text.

    Multi-word terms are matched as phrases. Single terms are matched as whole words with a
    simple singular/plural fold, and additionally by substring against the host blob, where
    word boundaries do not exist.
    """
    if " " in term:
        return f" {term} " in words or term.replace(" ", "") in host
    if f" {term} " in words or f" {term}s " in words:
        return True
    if len(term) >= 4 and term.endswith("s") and f" {term[:-1]} " in words:
        return True
    return len(term) >= 4 and term in host


def _group_hits(terms: frozenset[str], words: str, host: str) -> list[str]:
    return sorted(term for term in terms if _term_matches(term, words, host))


def classify_relevance(
    *, title: str, one_liner: str | None = None, url: str = ""
) -> tuple[RelevanceClass, float, dict[str, float], dict[str, list[str]]]:
    """Classify and score a story's topical relevance.

    Deterministic and inspectable: it reports which terms matched in each group, so the
    call can be checked by hand.

    Classification::

        direct     - an AI-automation signal AND (a business buyer OR an operational workflow)
        adjacent   - an AI-automation signal with neither
        irrelevant - no AI-automation signal at all

    The class is the primary ranking key, so a story naming only "AI agents" can never
    outrank one that also names a buyer or a workflow, regardless of score or popularity.
    A business tool with no AI signal is ``irrelevant`` rather than ``direct``: the buyer
    and workflow groups qualify an AI product, they do not substitute for one.

    Score::

        0.40 * ai + 0.25 * buyer + 0.35 * workflow

    where each group contributes ``min(matches, 2) / 2``.
    """
    words = _word_blob(title, one_liner or "", re.sub(r"[^a-z0-9]+", " ", urlsplit(url).path))
    host = _host_blob(url)

    matched = {
        "ai": _group_hits(AI_AUTOMATION_TERMS, words, host),
        "buyer": _group_hits(BUSINESS_BUYER_TERMS, words, host),
        "workflow": _group_hits(OPERATIONAL_WORKFLOW_TERMS, words, host),
    }
    components = {
        key: min(len(hits), _GROUP_SATURATION) / _GROUP_SATURATION for key, hits in matched.items()
    }
    score = round(
        _W_AI * components["ai"]
        + _W_BUYER * components["buyer"]
        + _W_WORKFLOW * components["workflow"],
        4,
    )

    if not matched["ai"]:
        relevance = RelevanceClass.IRRELEVANT
    elif matched["buyer"] or matched["workflow"]:
        relevance = RelevanceClass.DIRECT
    else:
        relevance = RelevanceClass.ADJACENT
    return relevance, score, components, matched


# --------------------------------------------------------------------------
# Quality (tie-breaking only)
# --------------------------------------------------------------------------

#: Weights of the quality components. They sum to 1.0. Quality never crosses a relevance
#: boundary - it only orders candidates that are already equally relevant.
_W_ENGAGEMENT = 0.55
_W_RECENCY = 0.25
_W_VARIANT = 0.20
assert abs(_W_ENGAGEMENT + _W_RECENCY + _W_VARIANT - 1.0) < 1e-9

#: A comment is weighted above an upvote: it takes more effort and better predicts a
#: thread containing third-party opinion worth reading.
_COMMENT_WEIGHT = 2.0

#: Engagement above this saturates. Past a few hundred weighted points the gap between a
#: big thread and a huge one says more about front-page luck than about the company.
_ENGAGEMENT_SATURATION = 500.0

_FRESH_UNTIL_DAYS = 30.0
_STALE_AFTER_DAYS = 720.0


def engagement_score(points: int, num_comments: int) -> float:
    """Log-compressed community engagement, normalised to 0-1."""
    weighted = max(points, 0) + _COMMENT_WEIGHT * max(num_comments, 0)
    if weighted <= 0:
        return 0.0
    return min(math.log10(1.0 + weighted) / math.log10(1.0 + _ENGAGEMENT_SATURATION), 1.0)


def recency_score(age_days: float) -> float:
    """Linear decay from 1.0 at 30 days old to 0.0 at 720 days old."""
    if age_days <= _FRESH_UNTIL_DAYS:
        return 1.0
    if age_days >= _STALE_AFTER_DAYS:
        return 0.0
    return (_STALE_AFTER_DAYS - age_days) / (_STALE_AFTER_DAYS - _FRESH_UNTIL_DAYS)


def quality_score(
    *, points: int, num_comments: int, age_days: float, variant_weight: float
) -> tuple[float, dict[str, float]]:
    """Composite tie-breaker::

    0.55 * engagement + 0.25 * recency + 0.20 * variant quality
    """
    engagement = engagement_score(points, num_comments)
    recency = recency_score(age_days)
    variant = min(max(variant_weight, 0.0), 1.0)
    score = _W_ENGAGEMENT * engagement + _W_RECENCY * recency + _W_VARIANT * variant
    return round(min(max(score, 0.0), 1.0), 4), {
        "engagement": round(engagement, 4),
        "recency": round(recency, 4),
        "variant": round(variant, 4),
    }


def discovery_rank(
    *,
    title: str,
    one_liner: str | None,
    url: str,
    points: int,
    num_comments: int,
    age_days: float,
    variant_weight: float,
) -> DiscoveryRank:
    """Build the full rank record for one story.

    Ranking is lexicographic - class, then relevance score, then quality - so engagement
    can only order candidates that are already equally relevant. See
    :data:`vc_scout.models.discovery.ORDERING_POLICY`.
    """
    relevance, relevance_value, relevance_parts, matched = classify_relevance(
        title=title, one_liner=one_liner, url=url
    )
    quality, quality_parts = quality_score(
        points=points, num_comments=num_comments, age_days=age_days, variant_weight=variant_weight
    )
    return DiscoveryRank(
        relevance_class=relevance,
        relevance_score=relevance_value,
        quality_score=quality,
        components={
            "relevance_ai": relevance_parts["ai"],
            "relevance_buyer": relevance_parts["buyer"],
            "relevance_workflow": relevance_parts["workflow"],
            **quality_parts,
        },
        matched=matched,
    )


# --------------------------------------------------------------------------
# Title parsing
# --------------------------------------------------------------------------

_PREFIX = re.compile(r"^\s*(show|launch|ask|tell)\s+hn\s*[:\-]\s*", re.IGNORECASE)
#: Batch tags such as "(YC W24)" describe funding, not the company's name.
_YC_TAG = re.compile(r"\s*\((?:yc\s+[a-z]?\d{2}|yc)\)\s*", re.IGNORECASE)
#: Separators used on HN between a product name and its description.
_SEPARATORS = re.compile(r"\s*[–—|]\s*|\s+-\s+|\s*:\s+|,\s+")


def parse_story_title(title: str) -> tuple[str, str | None]:
    """Split a Hacker News title into a company name and a one-liner.

    ``"Show HN: Acme Ops (YC W24) - reconcile invoices"`` becomes
    ``("Acme Ops", "reconcile invoices")``. Returns an empty name when nothing usable can
    be recovered; the caller falls back to the domain rather than inventing one.
    """
    stripped = _YC_TAG.sub(" ", _PREFIX.sub("", title)).strip()
    if not stripped:
        return "", None

    parts = _SEPARATORS.split(stripped, maxsplit=1)
    name = " ".join(parts[0].split())
    one_liner = " ".join(parts[1].split()) if len(parts) > 1 and parts[1].strip() else None

    # A "name" that is really a sentence is not a name. Hand the whole title back as the
    # one-liner and let the caller use the domain instead.
    if len(name.split()) > 5:
        return "", " ".join(stripped.split())
    return name, one_liner
