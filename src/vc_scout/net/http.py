"""A deliberately small, hardened HTTP client for reading public company pages.

Every URL this pipeline fetches comes from third-party text - a Hacker News link, or a
link found on a page that link pointed at. That makes fetching an attack surface, so the
rules are enforced here and nowhere else:

* http and https only, on their default ports;
* the hostname is resolved first and every resulting address must be public - loopback,
  private, link-local, multicast, reserved and unspecified ranges are refused;
* redirects are followed manually so that *every hop* is revalidated, and they are capped;
* the body is streamed and abandoned once it exceeds the byte ceiling, so an endless
  response cannot be downloaded before being rejected;
* only ``text/html`` is parsed;
* ``robots.txt`` is honoured.

Nothing here logs or persists request headers, cookies or credentials, and none are ever
sent: these are unauthenticated reads of public pages.
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx

from vc_scout.models.enums import FetchFailure

__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_REDIRECTS",
    "USER_AGENT",
    "FetchError",
    "FetchedPage",
    "SafeFetcher",
    "is_public_address",
]

USER_AGENT = (
    "vc-scout/0.1 (+https://github.com/ayushcodes10/vc-scout) "
    "investment-triage research crawler; respects robots.txt"
)

#: Sent on every request. No credential, cookie or authorization header is ever attached.
_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.1",
    "Accept-Language": "en",
}

DEFAULT_MAX_BYTES = 2_000_000
DEFAULT_MAX_REDIRECTS = 3
DEFAULT_CONNECT_TIMEOUT = 5.0
DEFAULT_READ_TIMEOUT = 15.0
#: Politeness gap between consecutive requests to the same host.
DEFAULT_HOST_DELAY = 0.5
#: robots.txt is small; refuse to read a large one rather than trusting it.
_ROBOTS_MAX_BYTES = 64_000

_ALLOWED_SCHEMES = frozenset({"http", "https"})
_DEFAULT_PORTS = {"http": 80, "https": 443}
_HTML_TYPES = ("text/html", "application/xhtml+xml")


class FetchError(Exception):
    """A page could not be fetched. Carries a category for the enrichment report."""

    def __init__(self, category: FetchFailure, detail: str, *, status: int | None = None) -> None:
        super().__init__(detail)
        self.category = category
        self.detail = detail
        self.status = status


@dataclass(frozen=True, slots=True)
class FetchedPage:
    """A successfully retrieved HTML document."""

    requested_url: str
    final_url: str
    status: int
    content_type: str
    body: str
    sha256: str
    bytes_read: int
    fetched_at: datetime
    redirects: tuple[str, ...] = ()
    #: True when the stream was cut at the byte ceiling rather than ending naturally.
    body_truncated: bool = False


def is_public_address(raw: str) -> bool:
    """Whether ``raw`` is an IP address safe to connect to.

    Everything that is not unambiguously public internet space is refused: loopback,
    private, link-local (which includes cloud metadata endpoints), multicast, reserved and
    unspecified. IPv4-mapped IPv6 addresses are unwrapped first so ``::ffff:127.0.0.1``
    cannot slip through.
    """
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _default_resolver(host: str) -> list[str]:
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    return [str(info[4][0]) for info in infos]


@dataclass(slots=True)
class SafeFetcher:
    """Fetches public HTML pages under strict, explicit limits."""

    client: httpx.Client | None = None
    resolver: Callable[[str], Sequence[str]] = _default_resolver
    max_bytes: int = DEFAULT_MAX_BYTES
    max_redirects: int = DEFAULT_MAX_REDIRECTS
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    read_timeout: float = DEFAULT_READ_TIMEOUT
    host_delay: float = DEFAULT_HOST_DELAY
    respect_robots: bool = True
    sleep: Callable[[float], None] = time.sleep
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)

    _owns_client: bool = field(default=False, init=False)
    _robots: dict[str, RobotFileParser | None] = field(default_factory=dict, init=False)
    _last_request: dict[str, float] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.client is None:
            self._owns_client = True
            self.client = httpx.Client(
                timeout=httpx.Timeout(self.read_timeout, connect=self.connect_timeout),
                follow_redirects=False,
                headers=_HEADERS,
            )

    # -- validation --------------------------------------------------------

    def validate(self, url: str) -> str:
        """Raise :class:`FetchError` unless ``url`` is safe to request.

        Returns the hostname. Called for the original URL and again for every redirect
        target, because a redirect is just another attacker-supplied URL.
        """
        try:
            split = urlsplit(url)
        except ValueError as exc:
            raise FetchError(FetchFailure.UNSAFE_URL, f"unparseable url: {exc}") from exc

        scheme = split.scheme.lower()
        if scheme not in _ALLOWED_SCHEMES:
            raise FetchError(FetchFailure.UNSAFE_URL, f"scheme {scheme!r} is not http(s)")

        host = split.hostname
        if not host:
            raise FetchError(FetchFailure.UNSAFE_URL, "url has no host")

        try:
            port = split.port
        except ValueError as exc:
            raise FetchError(FetchFailure.UNSAFE_URL, "url has an invalid port") from exc
        if port is not None and port != _DEFAULT_PORTS[scheme]:
            raise FetchError(
                FetchFailure.UNSAFE_URL, f"port {port} is not the default port for {scheme}"
            )

        try:
            addresses = list(self.resolver(host))
        except Exception as exc:  # noqa: BLE001 - any resolution failure is simply unsafe
            raise FetchError(
                FetchFailure.CONNECTION_ERROR, f"could not resolve {host}: {type(exc).__name__}"
            ) from exc

        if not addresses:
            raise FetchError(FetchFailure.UNSAFE_URL, f"{host} resolved to no addresses")
        for address in addresses:
            if not is_public_address(address):
                raise FetchError(
                    FetchFailure.UNSAFE_URL,
                    f"{host} resolves to the non-public address {address}",
                )
        return host

    # -- transport ---------------------------------------------------------

    def _throttle(self, host: str) -> None:
        if self.host_delay <= 0:
            return
        last = self._last_request.get(host)
        now = time.monotonic()
        if last is not None and (wait := self.host_delay - (now - last)) > 0:
            self.sleep(wait)
        self._last_request[host] = time.monotonic()

    def _open(self, url: str) -> tuple[httpx.Response, str, tuple[str, ...]]:
        """Follow redirects manually, revalidating each hop. Returns an open stream."""
        assert self.client is not None
        current = url
        redirects: list[str] = []

        for _ in range(self.max_redirects + 1):
            host = self.validate(current)
            self._throttle(host)
            try:
                request = self.client.build_request("GET", current, headers=_HEADERS)
                response = self.client.send(request, stream=True)
            except httpx.TimeoutException as exc:
                raise FetchError(FetchFailure.TIMEOUT, f"timed out fetching {current}") from exc
            except httpx.HTTPError as exc:
                raise FetchError(
                    FetchFailure.CONNECTION_ERROR, f"{type(exc).__name__} fetching {current}"
                ) from exc

            if response.is_redirect:
                location = response.headers.get("location", "")
                response.close()
                if not location:
                    raise FetchError(FetchFailure.HTTP_ERROR, "redirect without a location header")
                redirects.append(current)
                current = urljoin(current, location)
                continue

            if response.status_code in (401, 403):
                response.close()
                # A login wall or access control. Not something to work around.
                raise FetchError(
                    FetchFailure.BLOCKED,
                    f"access denied ({response.status_code})",
                    status=response.status_code,
                )
            if response.status_code >= 400:
                response.close()
                raise FetchError(
                    FetchFailure.HTTP_ERROR,
                    f"HTTP {response.status_code}",
                    status=response.status_code,
                )
            return response, current, tuple(redirects)

        raise FetchError(
            FetchFailure.TOO_MANY_REDIRECTS, f"more than {self.max_redirects} redirects"
        )

    def _read(self, response: httpx.Response, limit: int) -> tuple[bytes, bool]:
        """Stream at most ``limit`` bytes, abandoning anything larger."""
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in response.iter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total > limit:
                    # Stop pulling. The response is never fully downloaded.
                    return b"".join(chunks)[:limit], True
        except httpx.TimeoutException as exc:
            raise FetchError(FetchFailure.TIMEOUT, "timed out reading the response body") from exc
        except httpx.HTTPError as exc:
            raise FetchError(
                FetchFailure.CONNECTION_ERROR, f"{type(exc).__name__} reading the response body"
            ) from exc
        return b"".join(chunks), False

    # -- robots ------------------------------------------------------------

    def _robots_for(self, url: str) -> RobotFileParser | None:
        split = urlsplit(url)
        key = f"{split.scheme}://{split.netloc}"
        if key in self._robots:
            return self._robots[key]

        parser: RobotFileParser | None = None
        try:
            response, _, _ = self._open(f"{key}/robots.txt")
        except FetchError:
            # No readable robots.txt means no restriction to honour.
            parser = None
        else:
            try:
                body, _ = self._read(response, _ROBOTS_MAX_BYTES)
            except FetchError:
                body = b""
            finally:
                response.close()
            if body:
                parser = RobotFileParser()
                parser.parse(body.decode("utf-8", errors="replace").splitlines())
        self._robots[key] = parser
        return parser

    def allowed_by_robots(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parser = self._robots_for(url)
        return True if parser is None else parser.can_fetch(USER_AGENT, url)

    # -- public API --------------------------------------------------------

    def fetch_html(self, url: str) -> FetchedPage:
        """Fetch one HTML page, or raise :class:`FetchError` with a category."""
        if not self.allowed_by_robots(url):
            raise FetchError(FetchFailure.ROBOTS_DISALLOWED, f"robots.txt disallows {url}")

        response, final_url, redirects = self._open(url)
        try:
            content_type = response.headers.get("content-type", "")
            media_type = content_type.split(";", 1)[0].strip().lower()
            if media_type not in _HTML_TYPES:
                raise FetchError(
                    FetchFailure.NON_HTML,
                    f"content-type {media_type or 'unknown'!r} is not HTML",
                    status=response.status_code,
                )
            body, body_truncated = self._read(response, self.max_bytes)
            encoding = response.charset_encoding or "utf-8"
            status = response.status_code
        finally:
            response.close()

        try:
            text = body.decode(encoding, errors="replace")
        except LookupError:
            text = body.decode("utf-8", errors="replace")

        return FetchedPage(
            requested_url=url,
            final_url=final_url,
            status=status,
            content_type=media_type,
            body=text,
            sha256=hashlib.sha256(body).hexdigest(),
            bytes_read=len(body),
            fetched_at=self.clock(),
            redirects=redirects,
            body_truncated=body_truncated,
        )

    def close(self) -> None:
        if self._owns_client and self.client is not None:
            self.client.close()

    def __enter__(self) -> SafeFetcher:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()
