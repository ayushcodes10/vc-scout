"""Offline Hacker News responses.

Requests are dispatched to responses by the same (tags, query, optionalWords) triple the
real client sends, so production request construction is exercised rather than bypassed.
No socket is ever opened: ``tests/conftest.py`` blocks that outright, so a regression that
reintroduced a live call fails loudly instead of quietly reaching the network.

Two sources of data are used deliberately:

* committed JSON under ``tests/fixtures/hn/`` for the realistic end-to-end funnel, where
  the point is that the fixture looks like something Algolia actually returned;
* :func:`story` and the corpus builders for shortlist-composition tests, where the point
  is to control the exact number of direct and adjacent candidates.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx

from vc_scout.net.hn import HnAlgoliaClient, query_variants

__all__ = [
    "FIXTURE_DIR",
    "NOW",
    "QUERY",
    "adjacent_story",
    "direct_story",
    "load_fixture",
    "make_client",
    "make_transport",
    "story",
    "wrap",
]

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "hn"

#: The instant every fixture is dated against. Age-dependent assertions are relative to
#: this, so the suite does not rot as real time passes.
NOW = datetime(2026, 8, 1, tzinfo=UTC)

QUERY = "AI agents for SMB operations"


def load_fixture(name: str) -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((FIXTURE_DIR / f"{name}.json").read_text())
    return payload


# -- builders ----------------------------------------------------------------


def story(
    object_id: str,
    title: str,
    url: str | None,
    *,
    points: int = 10,
    comments: int = 3,
    days_ago: int = 20,
    tags: tuple[str, ...] = ("story", "show_hn"),
) -> dict[str, Any]:
    """One Algolia hit, dated relative to :data:`NOW`."""
    created = NOW - timedelta(days=days_ago)
    hit: dict[str, Any] = {
        "objectID": object_id,
        "title": title,
        "author": f"founder_{object_id}",
        "points": points,
        "num_comments": comments,
        "created_at": created.isoformat().replace("+00:00", "Z"),
        "created_at_i": int(created.timestamp()),
        "_tags": list(tags),
    }
    if url is not None:
        hit["url"] = url
    return hit


def wrap(hits: list[Any], query: str = QUERY) -> dict[str, Any]:
    return {
        "hits": hits,
        "nbHits": len(hits),
        "page": 0,
        "nbPages": 1,
        "hitsPerPage": 45,
        "query": query,
        "params": "advancedSyntax=true",
        "processingTimeMS": 4,
    }


def direct_story(index: int, **kwargs: Any) -> dict[str, Any]:
    """A story that must classify as ``direct``: AI automation plus buyer plus workflow.

    Each gets its own domain so domain deduplication does not interfere with composition
    tests.
    """
    return story(
        f"7{index:06d}",
        f"Show HN: Direct {index} - AI agent for invoicing for small business owners",
        f"https://direct-{index}.example/",
        **kwargs,
    )


def adjacent_story(index: int, **kwargs: Any) -> dict[str, Any]:
    """A story that must classify as ``adjacent``: AI automation, no buyer or workflow."""
    return story(
        f"8{index:06d}",
        f"Show HN: Generic {index} - an AI agent runtime",
        f"https://generic-{index}.example/",
        **kwargs,
    )


# -- transport ---------------------------------------------------------------


def _label_lookup() -> dict[tuple[str, str, bool], str]:
    """Map the request triple back to the variant label that produced it."""
    return {(v.tags, v.query, v.optional_words): v.label for v in query_variants(QUERY)}


def make_transport(
    responses: dict[str, dict[str, Any]] | None = None,
    *,
    overrides: dict[str, httpx.Response] | None = None,
    record: list[httpx.Request] | None = None,
    default_empty: bool = True,
) -> httpx.MockTransport:
    """Serve per-variant payloads.

    ``responses`` maps a variant label to a decoded body. Any variant without an entry
    serves an empty result set, so a test only has to describe the variants it cares about.
    """
    lookup = _label_lookup()
    payloads = responses or {}

    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        params = request.url.params
        key = (params.get("tags", ""), params.get("query", ""), "optionalWords" in params)
        label = lookup.get(key, "unknown")
        if overrides and label in overrides:
            return overrides[label]
        if label in payloads:
            return httpx.Response(200, json=payloads[label])
        if not default_empty:
            raise AssertionError(f"unexpected request for variant {label!r}")
        return httpx.Response(200, json=wrap([]))

    return httpx.MockTransport(handler)


def make_client(
    responses: dict[str, dict[str, Any]] | None = None, **kwargs: Any
) -> HnAlgoliaClient:
    """An :class:`HnAlgoliaClient` wired to fixtures instead of the network.

    Retry backoff is stubbed out: the retry *count* is asserted directly in
    ``test_hn_client.py``, so making the suite wait real seconds buys nothing.
    """
    return HnAlgoliaClient(
        httpx.Client(
            transport=make_transport(responses, **kwargs), base_url="https://hn.algolia.test"
        ),
        sleep=lambda _seconds: None,
    )
