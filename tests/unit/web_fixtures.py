"""Offline HTTP for the enrichment stage.

Every request is served by an ``httpx.MockTransport`` and every hostname is resolved by an
injected fake resolver, so neither DNS nor a socket is ever touched. ``tests/conftest.py``
blocks both outright, which means a regression that reintroduced real networking fails
loudly rather than quietly reaching the internet.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from vc_scout.net.http import SafeFetcher

__all__ = [
    "FIXTURE_DIR",
    "distinct_page",
    "NOW",
    "PUBLIC_IP",
    "fetcher",
    "html_response",
    "load_html",
    "make_transport",
    "public_resolver",
]

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "web"

NOW = datetime(2026, 8, 24, 9, 0, tzinfo=UTC)
PUBLIC_IP = "93.184.216.34"


def load_html(name: str) -> str:
    return (FIXTURE_DIR / f"{name}.html").read_text(encoding="utf-8")


def html_response(
    body: str, *, status: int = 200, content_type: str = "text/html"
) -> httpx.Response:
    return httpx.Response(status, text=body, headers={"content-type": content_type})


def public_resolver(_host: str) -> list[str]:
    """Every hostname resolves to one public address."""
    return [PUBLIC_IP]


def make_transport(
    routes: dict[str, httpx.Response | Callable[[httpx.Request], httpx.Response]],
    *,
    record: list[httpx.Request] | None = None,
    robots: str | None = None,
) -> httpx.MockTransport:
    """Serve ``routes`` keyed by absolute URL.

    Unrouted URLs return 404. ``robots.txt`` returns 404 by default, which means "no
    restriction recorded" - pass ``robots`` to serve a real policy instead.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(request)
        url = str(request.url)
        if url.endswith("/robots.txt"):
            if robots is None:
                return httpx.Response(404)
            return httpx.Response(200, text=robots, headers={"content-type": "text/plain"})
        route = routes.get(url)
        if route is None:
            return httpx.Response(404, text="not found")
        return route(request) if callable(route) else route

    return httpx.MockTransport(handler)


def fetcher(
    routes: dict[str, Any] | None = None,
    *,
    resolver: Callable[[str], Sequence[str]] = public_resolver,
    record: list[httpx.Request] | None = None,
    robots: str | None = None,
    transport: httpx.BaseTransport | None = None,
    **kwargs: Any,
) -> SafeFetcher:
    """A :class:`SafeFetcher` wired to fixtures, with throttling and the clock pinned."""
    transport = transport or make_transport(routes or {}, record=record, robots=robots)
    return SafeFetcher(
        client=httpx.Client(transport=transport, follow_redirects=False),
        resolver=resolver,
        host_delay=0.0,
        sleep=lambda _seconds: None,
        clock=lambda: NOW,
        **kwargs,
    )


def distinct_page(name: str) -> str:
    """A minimal HTML page whose body is unique to ``name``.

    Route maps need distinct bodies unless a test is specifically exercising content-hash
    deduplication, which would otherwise collapse them.
    """
    return (
        f"<!doctype html><html><head><title>{name} - Acme Ops</title></head>"
        f"<body><main><h1>{name}</h1>"
        f"<p>This is the {name} page for Acme Ops. It describes the {name} side of the "
        f"product in enough words to clear the minimum useful length for extraction.</p>"
        f"</main></body></html>"
    )
