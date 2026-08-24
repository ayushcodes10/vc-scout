"""Stable, content-derived identifiers.

Identifiers are deliberately *not* sequence numbers. A run that discovers the same page
twice, or discovers pages in a different order, must produce the same ``source_id`` and
the same ``evidence_id`` - otherwise citations cannot be compared across runs and the
replay guarantee in ``docs/PLAN.md`` is worthless.

Every ID is a SHA-256 digest of a normalised, canonical input, truncated to 12 hex
characters. Truncation is a readability trade-off; see ``docs/DECISIONS.md``.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

__all__ = [
    "COMPANY_ID_PATTERN",
    "RUN_ID_PATTERN",
    "company_id_for",
    "digest",
    "evidence_id_for",
    "is_valid_company_id",
    "is_valid_run_id",
    "normalize_url",
    "registrable_domain",
    "slugify",
    "source_id_for",
    "unknown_id_for",
]

_ID_HEX_LEN = 12

#: Identifiers used as path segments are restricted so they can never escape a run
#: directory or collide with reserved names.
COMPANY_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
RUN_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

#: Query parameters that carry no page identity, only campaign attribution.
_TRACKING_PARAMS = frozenset(
    {
        "fbclid",
        "gclid",
        "igshid",
        "mc_cid",
        "mc_eid",
        "msclkid",
        "ref",
        "ref_src",
        "utm_campaign",
        "utm_content",
        "utm_medium",
        "utm_source",
        "utm_term",
    }
)

_DEFAULT_PORTS = {"http": "80", "https": "443"}

_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
_SLUG_EDGES = re.compile(r"^-+|-+$")


def digest(*parts: str, length: int = _ID_HEX_LEN) -> str:
    """Return a stable hex digest over ``parts``.

    Parts are joined with a NUL byte so that ``("ab", "c")`` and ``("a", "bc")`` cannot
    collide.
    """
    joined = "\x00".join(parts).encode("utf-8")
    return hashlib.sha256(joined).hexdigest()[:length]


def slugify(value: str, *, max_length: int = 64) -> str:
    """Lowercase ASCII slug suitable for a filesystem path segment.

    Returns an empty string when ``value`` contains no usable characters; callers decide
    what to do about that rather than receiving a silently invented placeholder.
    """
    folded = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_STRIP.sub("-", folded.lower())
    slug = _SLUG_EDGES.sub("", slug)
    return slug[:max_length].rstrip("-")


def normalize_url(url: str) -> str:
    """Canonicalise a URL so that cosmetically different spellings share one identity.

    Lowercases scheme and host, drops the default port, drops the fragment, drops known
    tracking parameters, sorts the remaining query, and removes a trailing slash on
    non-root paths. Does not resolve redirects - that requires the network.
    """
    split = urlsplit(url.strip())
    scheme = split.scheme.lower()
    host = split.hostname or ""
    port = split.port

    netloc = host
    if port is not None and _DEFAULT_PORTS.get(scheme) != str(port):
        netloc = f"{host}:{port}"

    query = urlencode(
        sorted(
            (k, v)
            for k, v in parse_qsl(split.query, keep_blank_values=True)
            if k.lower() not in _TRACKING_PARAMS
        )
    )

    path = split.path or "/"
    if len(path) > 1:
        path = path.rstrip("/") or "/"

    return urlunsplit((scheme, netloc, path, query, ""))


def registrable_domain(url: str) -> str:
    """Best-effort registrable domain, with any leading ``www.`` removed.

    This is a heuristic, not a public-suffix lookup; ``docs/LIMITATIONS.md`` records the
    cases it gets wrong (for example ``example.co.uk`` collapsing to ``co.uk``).
    """
    host = (urlsplit(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def source_id_for(url: str) -> str:
    """Stable ID for a source, derived only from its normalised URL."""
    return f"src-{digest(normalize_url(url))}"


def company_id_for(name: str, website: str | None = None) -> str:
    """Stable, path-safe ID for a company.

    Prefers a slug of the company name for readability. Falls back to the website's
    domain, then to a digest, so that an unnameable candidate still gets a valid ID
    rather than being dropped.
    """
    slug = slugify(name)
    if slug and COMPANY_ID_PATTERN.match(slug):
        return slug
    if website:
        domain_slug = slugify(registrable_domain(website))
        if domain_slug and COMPANY_ID_PATTERN.match(domain_slug):
            return domain_slug
    return f"c-{digest(name, website or '')}"


def evidence_id_for(company_id: str, claim: str, source_ids: list[str]) -> str:
    """Stable ID for an evidence claim.

    Source IDs are sorted before hashing so that citation order does not change identity.
    Claim text is whitespace-normalised for the same reason.
    """
    normalised_claim = " ".join(claim.split())
    return f"ev-{digest(company_id, normalised_claim, *sorted(source_ids))}"


def is_valid_company_id(value: str) -> bool:
    return bool(COMPANY_ID_PATTERN.match(value))


def is_valid_run_id(value: str) -> bool:
    return bool(RUN_ID_PATTERN.match(value))


def unknown_id_for(company_id: str, question: str) -> str:
    """Stable ID for a recorded unknown.

    Unknowns are persisted without identifiers, so analysis derives one from the question
    text when it hands the dossier to the model. Content-derived, like every other ID here,
    so the same unknown always carries the same reference and a reference cannot be
    invented.
    """
    return f"unk-{digest(company_id, ' '.join(question.split()))}"
