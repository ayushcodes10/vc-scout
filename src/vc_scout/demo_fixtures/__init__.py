"""Committed fixtures for the offline demo.

The demo has to prove the pipeline, not simulate it. So it runs the same orchestrator, the
same stages and the same production HTTP and Algolia clients - only the transport
underneath them is swapped for one that serves these two committed files. Nothing about
request construction, URL safety, redirect handling, robots or extraction is bypassed.

Two files, both diffable:

* ``hn.json`` - one Algolia-shaped payload of ten Show HN stories on distinct domains;
* ``pages.json`` - a homepage, a pricing page and an about page for each of them.

The corpus is deliberately mixed. Most entries are SMB workflow products the thesis is
about; one is an open-source agent runtime, which the analysis should place *outside* the
thesis on evidence. A demo where everything scores the same proves nothing.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx

from vc_scout.net.hn import HnAlgoliaClient
from vc_scout.net.http import SafeFetcher

__all__ = [
    "DEMO_QUERY",
    "demo_client",
    "demo_fetcher",
    "demo_pages",
    "demo_stories",
]

_DIR = Path(__file__).resolve().parent

#: The query the demo run records. It is the query these fixtures answer.
DEMO_QUERY = "AI customer support and back-office automation for small businesses"

#: Every fixture host resolves here. A public address, so the SSRF guard is exercised
#: rather than sidestepped.
_PUBLIC_IP = "93.184.216.34"


@lru_cache(maxsize=1)
def demo_stories() -> dict[str, Any]:
    payload: dict[str, Any] = json.loads((_DIR / "hn.json").read_text(encoding="utf-8"))
    return payload


@lru_cache(maxsize=1)
def demo_pages() -> dict[str, str]:
    pages: dict[str, str] = json.loads((_DIR / "pages.json").read_text(encoding="utf-8"))
    return pages


def _hn_handler(_request: httpx.Request) -> httpx.Response:
    """Serve the same corpus to every query variant.

    Deduplication by domain and the relevance gate then do their real work on it, which is
    the point: the funnel a reviewer sees in the demo is the production funnel.
    """
    return httpx.Response(200, json=demo_stories())


def _page_handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url.endswith("/robots.txt"):
        # 404 means "no policy recorded", which is what most small sites actually return.
        return httpx.Response(404)
    body = demo_pages().get(url)
    if body is None:
        return httpx.Response(404, text="not found")
    return httpx.Response(200, text=body, headers={"content-type": "text/html; charset=utf-8"})


def demo_client() -> HnAlgoliaClient:
    """The production Algolia client over the committed story corpus."""
    return HnAlgoliaClient(
        httpx.Client(transport=httpx.MockTransport(_hn_handler)), sleep=lambda _seconds: None
    )


def demo_fetcher() -> SafeFetcher:
    """The production page fetcher over the committed page corpus."""
    return SafeFetcher(
        client=httpx.Client(transport=httpx.MockTransport(_page_handler), follow_redirects=False),
        resolver=lambda _host: [_PUBLIC_IP],
        sleep=lambda _seconds: None,
    )
