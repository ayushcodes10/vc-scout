"""The hardened fetcher.

These are the security tests for the stage. Every URL enrichment touches originates in
third-party text, so the guards here are the difference between reading a public marketing
page and being pointed at a cloud metadata endpoint.
"""

from __future__ import annotations

import httpx
import pytest

from tests.unit.web_fixtures import (
    NOW,
    PUBLIC_IP,
    fetcher,
    html_response,
    load_html,
    public_resolver,
)
from vc_scout.models.enums import FetchFailure
from vc_scout.net.http import USER_AGENT, FetchError, SafeFetcher, is_public_address

HOME = "https://acme.example/"


def fetch(url: str = HOME, **kwargs: object) -> object:
    return fetcher({HOME: html_response(load_html("homepage"))}, **kwargs).fetch_html(url)  # type: ignore[arg-type]


# -- address safety ----------------------------------------------------------


@pytest.mark.parametrize("address", ["93.184.216.34", "8.8.8.8", "2606:4700::1111"])
def test_public_addresses_are_allowed(address: str) -> None:
    assert is_public_address(address)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",  # loopback
        "::1",  # loopback, v6
        "10.0.0.5",  # private
        "192.168.1.10",  # private
        "172.16.4.4",  # private
        "169.254.169.254",  # link-local: the cloud metadata endpoint
        "0.0.0.0",  # noqa: S104 - unspecified address, tested precisely because it is unsafe
        "224.0.0.1",  # multicast
        "240.0.0.1",  # reserved
        "::ffff:127.0.0.1",  # IPv4-mapped loopback
        "not-an-address",
    ],
)
def test_restricted_addresses_are_refused(address: str) -> None:
    assert not is_public_address(address)


def test_an_initial_private_address_is_rejected_before_any_request() -> None:
    seen: list[httpx.Request] = []
    client = fetcher({}, resolver=lambda _h: ["127.0.0.1"], record=seen)
    with pytest.raises(FetchError) as exc:
        client.fetch_html("https://localhost.example/")
    assert exc.value.category is FetchFailure.UNSAFE_URL
    assert seen == [], "no request may be issued to a non-public address"


def test_dns_resolving_to_a_private_address_is_rejected() -> None:
    """The hostname looks public; only resolution reveals it is not."""
    seen: list[httpx.Request] = []
    client = fetcher(
        {HOME: html_response("<html/>")}, resolver=lambda _h: ["10.1.2.3"], record=seen
    )
    with pytest.raises(FetchError, match="non-public address"):
        client.fetch_html(HOME)
    assert seen == []


def test_a_host_resolving_to_a_mix_is_rejected_on_the_worst_address() -> None:
    client = fetcher({}, resolver=lambda _h: [PUBLIC_IP, "169.254.169.254"])
    with pytest.raises(FetchError, match="169.254.169.254"):
        client.fetch_html(HOME)


@pytest.mark.parametrize(
    "url",
    [
        "ftp://acme.example/",
        "file:///etc/passwd",
        "javascript:alert(1)",
        "https:///nohost",
        "http://acme.example:8080/",
    ],
)
def test_unsafe_schemes_and_ports_are_rejected(url: str) -> None:
    with pytest.raises(FetchError) as exc:
        fetcher({}).fetch_html(url)
    assert exc.value.category is FetchFailure.UNSAFE_URL


def test_a_resolution_failure_is_a_connection_error_not_a_crash() -> None:
    def boom(_host: str) -> list[str]:
        raise OSError("nxdomain")

    with pytest.raises(FetchError) as exc:
        fetcher({}, resolver=boom).fetch_html(HOME)
    assert exc.value.category is FetchFailure.CONNECTION_ERROR


# -- redirects ---------------------------------------------------------------


def test_redirects_are_followed_and_the_final_url_is_recorded() -> None:
    routes = {
        "https://acme.example/start": httpx.Response(301, headers={"location": "/final"}),
        "https://acme.example/final": html_response(load_html("homepage")),
    }
    page = fetcher(routes).fetch_html("https://acme.example/start")
    assert page.final_url == "https://acme.example/final"
    assert page.redirects == ("https://acme.example/start",)


def test_every_redirect_hop_is_revalidated() -> None:
    """A safe first URL must not become a free pass to an unsafe second one."""
    hosts: list[str] = []

    def resolver(host: str) -> list[str]:
        hosts.append(host)
        return ["169.254.169.254"] if host == "metadata.example" else [PUBLIC_IP]

    routes = {
        "https://acme.example/": httpx.Response(
            302, headers={"location": "https://metadata.example/latest/meta-data/"}
        )
    }
    with pytest.raises(FetchError) as exc:
        fetcher(routes, resolver=resolver).fetch_html(HOME)
    assert exc.value.category is FetchFailure.UNSAFE_URL
    assert "metadata.example" in hosts


def test_redirect_chains_are_capped() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": f"/hop{len(str(request.url))}"})

    client = fetcher({}, transport=httpx.MockTransport(handler), max_redirects=2)
    with pytest.raises(FetchError) as exc:
        client.fetch_html(HOME)
    assert exc.value.category is FetchFailure.TOO_MANY_REDIRECTS


def test_a_redirect_without_a_location_is_an_http_error() -> None:
    with pytest.raises(FetchError) as exc:
        fetcher({HOME: httpx.Response(302)}).fetch_html(HOME)
    assert exc.value.category is FetchFailure.HTTP_ERROR


# -- content rules -----------------------------------------------------------


@pytest.mark.parametrize(
    "content_type", ["application/pdf", "application/json", "text/plain", "image/png", ""]
)
def test_only_html_is_parsed(content_type: str) -> None:
    routes = {HOME: httpx.Response(200, text="x", headers={"content-type": content_type})}
    with pytest.raises(FetchError) as exc:
        fetcher(routes).fetch_html(HOME)
    assert exc.value.category is FetchFailure.NON_HTML


def test_xhtml_is_accepted() -> None:
    routes = {
        HOME: html_response(
            "<html><body><p>hello</p></body></html>", content_type="application/xhtml+xml"
        )
    }
    assert fetcher(routes).fetch_html(HOME).content_type == "application/xhtml+xml"


def test_an_oversized_response_is_cut_while_streaming() -> None:
    """The body is abandoned mid-stream, not downloaded and then measured."""
    delivered = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        def stream() -> object:
            nonlocal delivered
            for _ in range(100):
                delivered += 1_000
                yield b"x" * 1_000

        return httpx.Response(200, headers={"content-type": "text/html"}, content=stream())

    client = fetcher({}, transport=httpx.MockTransport(handler), max_bytes=5_000)
    page = client.fetch_html(HOME)
    assert page.body_truncated is True
    assert page.bytes_read == 5_000
    assert delivered < 100_000, "streaming stopped early rather than reading the whole body"


def test_a_timeout_is_categorised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=request)

    with pytest.raises(FetchError) as exc:
        fetcher({}, transport=httpx.MockTransport(handler)).fetch_html(HOME)
    assert exc.value.category is FetchFailure.TIMEOUT


def test_a_connection_failure_is_categorised() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(FetchError) as exc:
        fetcher({}, transport=httpx.MockTransport(handler)).fetch_html(HOME)
    assert exc.value.category is FetchFailure.CONNECTION_ERROR


@pytest.mark.parametrize(
    ("status", "category"),
    [(401, "blocked"), (403, "blocked"), (404, "http_error"), (500, "http_error")],
)
def test_error_statuses_are_categorised(status: int, category: str) -> None:
    with pytest.raises(FetchError) as exc:
        fetcher({HOME: httpx.Response(status)}).fetch_html(HOME)
    assert exc.value.category.value == category
    assert exc.value.status == status


def test_login_walls_are_not_worked_around() -> None:
    """A 403 is a decision by the site, and it is respected rather than retried."""
    seen: list[httpx.Request] = []
    with pytest.raises(FetchError, match="access denied"):
        fetcher({HOME: httpx.Response(403)}, record=seen).fetch_html(HOME)
    assert len(seen) == 2, "one robots.txt probe and one page request, with no retry"


# -- robots ------------------------------------------------------------------


def test_robots_disallow_is_honoured() -> None:
    client = fetcher({HOME: html_response("<html/>")}, robots="User-agent: *\nDisallow: /")
    with pytest.raises(FetchError) as exc:
        client.fetch_html(HOME)
    assert exc.value.category is FetchFailure.ROBOTS_DISALLOWED


def test_a_missing_robots_file_means_no_restriction() -> None:
    page = fetcher({HOME: html_response(load_html("homepage"))}).fetch_html(HOME)
    assert page.status == 200


def test_robots_is_fetched_once_per_host() -> None:
    seen: list[httpx.Request] = []
    routes = {
        HOME: html_response(load_html("homepage")),
        "https://acme.example/pricing": html_response(load_html("pricing")),
    }
    client = fetcher(routes, record=seen, robots="User-agent: *\nAllow: /")
    client.fetch_html(HOME)
    client.fetch_html("https://acme.example/pricing")
    assert sum(1 for r in seen if str(r.url).endswith("/robots.txt")) == 1


# -- request hygiene ---------------------------------------------------------


def test_requests_identify_themselves_and_carry_no_credentials() -> None:
    seen: list[httpx.Request] = []
    fetcher({HOME: html_response(load_html("homepage"))}, record=seen).fetch_html(HOME)
    page_request = next(r for r in seen if not str(r.url).endswith("/robots.txt"))

    assert page_request.headers["user-agent"] == USER_AGENT
    assert "vc-scout" in USER_AGENT
    sent = {name.lower() for name in page_request.headers}
    assert sent.isdisjoint({"authorization", "cookie", "proxy-authorization", "x-api-key"})


def test_the_page_record_carries_a_content_hash_and_timestamp() -> None:
    page = fetcher({HOME: html_response(load_html("homepage"))}).fetch_html(HOME)
    assert len(page.sha256) == 64
    assert page.fetched_at == NOW
    assert page.status == 200
    assert page.content_type == "text/html"


def test_the_default_fetcher_would_need_real_dns_and_therefore_cannot_run_here() -> None:
    """Guards the injection seam: the default resolver is the real one."""
    with pytest.raises(FetchError) as exc:
        SafeFetcher(
            client=httpx.Client(transport=httpx.MockTransport(lambda _request: httpx.Response(200)))
        ).fetch_html(HOME)
    assert exc.value.category is FetchFailure.CONNECTION_ERROR


def test_public_resolver_fixture_matches_the_real_signature() -> None:
    assert public_resolver("anything") == [PUBLIC_IP]
