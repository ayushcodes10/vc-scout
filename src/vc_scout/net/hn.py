"""Hacker News Algolia search client.

The client is a thin, injectable wrapper: it owns request construction, retries and
response shape, and nothing else. Callers pass in an ``httpx.Client``, which is how the
test suite exercises this code against fixtures with no network access.

API reference: https://hn.algolia.com/api - public, unauthenticated, no credential is
sent and none is required.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

__all__ = [
    "DEFAULT_BASE_URL",
    "HnAlgoliaClient",
    "HnError",
    "INTENT_FACETS",
    "HnStory",
    "MalformedHitError",
    "QueryVariant",
    "parse_hit",
    "query_variants",
]

DEFAULT_BASE_URL = "https://hn.algolia.com/api/v1"
USER_AGENT = "vc-scout/0.1 (investment triage; +https://github.com/ayushcodes10/vc-scout)"
_HEADERS = {"User-Agent": USER_AGENT, "Accept": "application/json"}

_CONNECT_TIMEOUT = 5.0
_READ_TIMEOUT = 15.0
#: One retry only, on transient failures. Matches the retry budget used elsewhere.
_MAX_ATTEMPTS = 2
_RETRY_STATUS = frozenset({429, 500, 502, 503, 504})
_BACKOFF_SECONDS = 0.5


class HnError(RuntimeError):
    """A request to the Algolia API could not be completed."""


class MalformedHitError(ValueError):
    """A single search hit could not be parsed. Never fatal to a run."""


@dataclass(frozen=True, slots=True)
class QueryVariant:
    """One deterministic search against the Algolia API.

    ``weight`` feeds the discovery rank: a Show HN launch is a stronger signal that the
    link points at a real product than a generic story match is.

    ``optional_words`` controls recall. Algolia requires *every* query word by default,
    which for a phrase like "AI agents for SMB operations" matches almost nothing. Sending
    the words as optional turns the search into a relevance-ranked OR: hits matching more
    of the query rank first, but the long tail is still reachable.
    """

    label: str
    query: str
    tags: str
    endpoint: str
    weight: float
    optional_words: bool = False


#: The firm's thesis surface, as a bounded family of workflow and buyer facets. These
#: encode *what the fund is looking for*, not what the operator typed, which is why they
#: are fixed rather than derived from the query. Each is combined with whatever
#: AI-automation wording the query itself uses, so the family stays query-aware without
#: depending on the operator having named every workflow.
#:
#: Kept deliberately small: nine facets plus three query-faithful searches is twelve
#: requests per run, and every one of them is listed in the sourcing report.
INTENT_FACETS: tuple[tuple[str, str, float], ...] = (
    ("smb", "SMB small business", 0.95),
    ("small-business", "small business owners", 0.95),
    ("business-operations", "business operations", 0.95),
    ("customer-support", "customer support service helpdesk", 0.9),
    ("sales", "sales CRM leads outreach", 0.9),
    ("finance-accounting", "finance accounting bookkeeping invoicing", 0.9),
    ("scheduling", "scheduling booking dispatch appointments", 0.9),
    ("back-office", "back office admin workflow paperwork", 0.9),
    ("ecommerce-retail", "ecommerce retail store inventory orders", 0.9),
)

#: Wording used to express "AI automation" when the query supplies none of its own.
_DEFAULT_AI_STEM = "AI agents"

#: Query words that signal AI automation, used to build the facet searches. Kept local to
#: avoid a dependency from the network layer onto the discovery rules.
_AI_STEM_WORDS = frozenset(
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
        "llm",
        "gpt",
    }
)


def _ai_stem(query: str) -> str:
    """The AI-automation wording present in the query, or a sensible default.

    Facet searches are built as "<ai stem> <facet>", so a query phrased around
    "automation" produces "automation scheduling booking appointments" rather than
    silently switching to someone else's vocabulary.
    """
    words = [w for w in re.split(r"[^A-Za-z0-9]+", query) if w]
    stem = [w for w in words if w.lower() in _AI_STEM_WORDS]
    return " ".join(stem) if stem else _DEFAULT_AI_STEM


def query_variants(query: str) -> tuple[QueryVariant, ...]:
    """Expand a partner's query into a bounded, deterministic family of searches.

    Twelve searches, always in this order:

    * three query-faithful searches - the operator's exact words against Show HN (all words
      required), Launch HN, and the general story index;
    * nine intent facets - the query's AI-automation wording combined with a specific
      buyer or workflow, each searched across Show HN and Launch HN together.

    Facet searches send their words as optional. Requiring every word would return almost
    nothing, and a genuinely relevant product may describe its workflow without ever using
    the operator's phrasing - a scheduling agent for salons will not say "SMB operations".
    Recall is bought here and paid for by the relevance gate, not by hoping the query
    happens to match.
    """
    cleaned = " ".join(query.split())
    stem = _ai_stem(cleaned)

    variants = [
        QueryVariant(
            label="query-show-hn",
            query=cleaned,
            tags="show_hn",
            endpoint="search",
            weight=1.0,
        ),
        QueryVariant(
            label="query-launch-hn",
            query=cleaned,
            tags="launch_hn",
            endpoint="search",
            weight=0.85,
            optional_words=True,
        ),
        QueryVariant(
            label="query-story",
            query=cleaned,
            tags="story",
            endpoint="search",
            weight=0.5,
            optional_words=True,
        ),
    ]
    variants.extend(
        QueryVariant(
            label=f"intent-{slug}",
            query=f"{stem} {facet}",
            # Show HN and Launch HN together: the OR form of Algolia's tag filter.
            tags="(show_hn,launch_hn)",
            endpoint="search",
            weight=weight,
            optional_words=True,
        )
        for slug, facet, weight in INTENT_FACETS
    )
    return tuple(variants)


@dataclass(frozen=True, slots=True)
class HnStory:
    """A parsed Algolia story hit."""

    object_id: str
    title: str
    url: str
    points: int
    num_comments: int
    created_at: datetime
    author: str | None = None
    tags: tuple[str, ...] = ()

    @property
    def discussion_url(self) -> str:
        """Permalink to the Hacker News thread - the citable source for this story."""
        return f"https://news.ycombinator.com/item?id={self.object_id}"

    def age_days(self, now: datetime) -> float:
        return max((now - self.created_at).total_seconds() / 86400.0, 0.0)


def _require_int(hit: dict[str, Any], key: str) -> int:
    value = hit.get(key)
    if value is None:
        return 0
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedHitError(f"{key!r} is {type(value).__name__}, expected int")
    if value < 0:
        raise MalformedHitError(f"{key!r} is negative")
    return value


def parse_hit(hit: Any) -> HnStory:
    """Parse one Algolia hit into an :class:`HnStory`.

    Raises :class:`MalformedHitError` rather than returning a partially-populated story, so a
    single bad record is recorded and skipped instead of poisoning the candidate set.
    """
    if not isinstance(hit, dict):
        raise MalformedHitError(f"hit is {type(hit).__name__}, expected object")

    object_id = hit.get("objectID")
    if not isinstance(object_id, str) or not object_id.strip():
        raise MalformedHitError("missing or non-string objectID")

    title = hit.get("title") or hit.get("story_title")
    if not isinstance(title, str) or not title.strip():
        raise MalformedHitError(f"story {object_id} has no usable title")

    url = hit.get("url") or hit.get("story_url")
    if not isinstance(url, str) or not url.strip():
        # Ask HN and text-only posts legitimately have no URL. Not an error, but not a
        # candidate either; the caller records it as a rejection.
        raise MalformedHitError(f"story {object_id} has no external url")

    raw_created = hit.get("created_at_i")
    if isinstance(raw_created, int) and not isinstance(raw_created, bool):
        created_at = datetime.fromtimestamp(raw_created, tz=UTC)
    else:
        raw_iso = hit.get("created_at")
        if not isinstance(raw_iso, str):
            raise MalformedHitError(f"story {object_id} has no usable creation timestamp")
        try:
            created_at = datetime.fromisoformat(raw_iso.replace("Z", "+00:00"))
        except ValueError as exc:
            raise MalformedHitError(f"story {object_id} has an unparseable created_at") from exc
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)

    raw_tags = hit.get("_tags")
    tags: tuple[str, ...] = ()
    if isinstance(raw_tags, list):
        tags = tuple(tag for tag in raw_tags if isinstance(tag, str))

    author = hit.get("author")
    return HnStory(
        object_id=object_id.strip(),
        title=title.strip(),
        url=url.strip(),
        points=_require_int(hit, "points"),
        num_comments=_require_int(hit, "num_comments"),
        created_at=created_at.astimezone(UTC),
        author=author.strip() if isinstance(author, str) and author.strip() else None,
        tags=tags,
    )


class HnAlgoliaClient:
    """Issues searches against the public Algolia index."""

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        sleep: object = time.sleep,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.Client(
            timeout=httpx.Timeout(_READ_TIMEOUT, connect=_CONNECT_TIMEOUT),
            follow_redirects=True,
        )
        self._sleep = sleep

    def search(
        self,
        variant: QueryVariant,
        *,
        hits_per_page: int,
        page: int = 0,
        since_unix: int | None = None,
    ) -> dict[str, Any]:
        """Run one search and return the decoded response body."""
        params: dict[str, str] = {
            "query": variant.query,
            "tags": variant.tags,
            "hitsPerPage": str(hits_per_page),
            "page": str(page),
        }
        if variant.optional_words:
            params["optionalWords"] = variant.query
        if since_unix is not None:
            params["numericFilters"] = f"created_at_i>{since_unix}"

        url = f"{self.base_url}/{variant.endpoint}"
        last_error: str = "no attempt was made"
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                # Headers go on the request, not the client, so an injected client
                # still identifies itself honestly.
                response = self._client.get(url, params=params, headers=_HEADERS)
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            else:
                if response.status_code in _RETRY_STATUS:
                    last_error = f"HTTP {response.status_code}"
                elif response.status_code >= 400:
                    raise HnError(f"{variant.label}: HTTP {response.status_code}")
                else:
                    try:
                        body = response.json()
                    except ValueError as exc:
                        raise HnError(f"{variant.label}: response was not JSON") from exc
                    if not isinstance(body, dict):
                        raise HnError(f"{variant.label}: response was not a JSON object")
                    return body
            if attempt < _MAX_ATTEMPTS:
                self._sleep(_BACKOFF_SECONDS)  # type: ignore[operator]
        raise HnError(f"{variant.label}: {last_error}")

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> HnAlgoliaClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
