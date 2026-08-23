"""HTML to readable text, and deterministic internal-link selection.

Extraction prefers a page's main content over its chrome: navigation, headers, footers and
asides are removed, and a ``<main>`` or ``<article>`` element is used as the root when one
exists. What survives is the title, the headings and the meaningful block-level text, in
document order, whitespace-normalised and bounded.

Link selection is deterministic by construction - same-origin only, matched against a fixed
priority of page roles, ties broken by shortest path then alphabetically - so the same page
always yields the same three follow-ups.

Everything here treats the HTML as hostile input: it is parsed, never executed, and no
script, style or embedded content survives into the extracted text.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup, Tag

from vc_scout.models.enums import PageRole
from vc_scout.util.ids import normalize_url

__all__ = [
    "MAX_HEADINGS",
    "MAX_TEXT_CHARS",
    "EXTRACTOR",
    "ExtractedContent",
    "InternalLink",
    "extract_content",
    "role_for_path",
    "select_internal_links",
]

EXTRACTOR = "bs4-main-content/1"

#: Per-page ceiling on extracted text. Enough for an about or pricing page several times
#: over; small enough that one verbose blog post cannot dominate a company's evidence.
MAX_TEXT_CHARS = 20_000
MAX_HEADINGS = 30

#: Elements that never carry a company's own description.
_NOISE_TAGS = ("script", "style", "noscript", "template", "svg", "iframe", "form", "canvas")
_CHROME_TAGS = ("nav", "header", "footer", "aside")

#: Block elements whose text is worth keeping, read in document order.
_BLOCK_TAGS = ("h1", "h2", "h3", "h4", "p", "li", "blockquote", "dd", "figcaption")
_HEADING_TAGS = ("h1", "h2", "h3")

#: Shortest run of characters worth treating as a sentence fragment rather than a label.
_MIN_BLOCK_CHARS = 3

_WHITESPACE = re.compile(r"\s+")

#: Page roles in crawl priority. Pricing and customers sit high because they carry the
#: clearest evidence of who pays and for what; blog sits last because it is usually the
#: largest and least specific surface on a startup site.
_ROLE_PATTERNS: tuple[tuple[PageRole, tuple[str, ...]], ...] = (
    (
        PageRole.PRODUCT,
        (
            "/product",
            "/products",
            "/features",
            "/platform",
            "/solutions",
            "/how-it-works",
            "/how_it_works",
            "/use-cases",
        ),
    ),
    (PageRole.PRICING, ("/pricing", "/price", "/plans", "/plan")),
    (
        PageRole.CUSTOMERS,
        ("/customers", "/customer", "/case-stud", "/case_stud", "/testimonial", "/stories"),
    ),
    (PageRole.ABOUT, ("/about", "/about-us", "/company", "/our-story", "/mission")),
    (PageRole.TEAM, ("/team", "/founders", "/people", "/leadership", "/who-we-are")),
    (PageRole.CHANGELOG, ("/changelog", "/releases", "/release-notes", "/whats-new", "/updates")),
    (PageRole.BLOG, ("/blog", "/news", "/posts", "/articles")),
)

#: Path fragments that are never worth a fetch even when they match a role above.
_EXCLUDED_FRAGMENTS = (
    "/login",
    "/signin",
    "/sign-in",
    "/signup",
    "/sign-up",
    "/register",
    "/auth",
    "/account",
    "/dashboard",
    "/app/",
    "/admin",
    "/cart",
    "/checkout",
    "/privacy",
    "/terms",
    "/legal",
    "/cookie",
    "/careers",
    "/jobs",
    "/rss",
    "/feed",
)

#: File extensions that are not HTML pages.
_EXCLUDED_SUFFIXES = (
    ".pdf",
    ".zip",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".svg",
    ".webp",
    ".mp4",
    ".mp3",
    ".xml",
    ".json",
    ".css",
    ".js",
    ".ico",
    ".txt",
    ".dmg",
    ".exe",
)


@dataclass(frozen=True, slots=True)
class ExtractedContent:
    """Readable text recovered from one HTML document."""

    title: str | None
    headings: list[str]
    text: str
    truncated: bool
    extractor: str = EXTRACTOR


@dataclass(frozen=True, slots=True)
class InternalLink:
    """A same-origin link worth following."""

    url: str
    role: PageRole
    anchor: str = ""


def _normalise(text: str) -> str:
    return _WHITESPACE.sub(" ", text).strip()


def _soup(html: str) -> BeautifulSoup:
    return BeautifulSoup(html, "html.parser")


def role_for_path(path: str) -> PageRole | None:
    """Which page role a URL path indicates, or ``None``.

    Matched in the declared priority order, so a path matching two roles is assigned the
    higher-priority one deterministically.
    """
    lowered = path.lower()
    for role, patterns in _ROLE_PATTERNS:
        if any(pattern in lowered for pattern in patterns):
            return role
    return None


def extract_content(html: str, *, max_chars: int = MAX_TEXT_CHARS) -> ExtractedContent:
    """Recover title, headings and main text from an HTML document.

    Returns empty content rather than raising when a document has no readable text; the
    caller decides whether that counts as a failure.
    """
    soup = _soup(html)

    raw_title = soup.title.get_text() if soup.title else None
    title = _normalise(raw_title) if raw_title else None
    if not title:
        meta_title = soup.find("meta", attrs={"property": "og:title"})
        if isinstance(meta_title, Tag):
            title = _normalise(str(meta_title.get("content") or "")) or None

    description = ""
    meta_description = soup.find("meta", attrs={"name": "description"})
    if isinstance(meta_description, Tag):
        description = _normalise(str(meta_description.get("content") or ""))

    for tag_name in _NOISE_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()
    for tag_name in _CHROME_TAGS:
        for tag in soup.find_all(tag_name):
            tag.decompose()

    root: Tag | BeautifulSoup = soup
    for candidate in (
        soup.find("main"),
        soup.find("article"),
        soup.find(True, attrs={"role": "main"}),
        soup.body,
    ):
        if isinstance(candidate, Tag):
            root = candidate
            break

    headings: list[str] = []
    blocks: list[str] = []
    seen: set[str] = set()
    if description:
        blocks.append(description)
        seen.add(description)

    for element in root.find_all(list(_BLOCK_TAGS)):
        # A list item wrapping a paragraph would otherwise be emitted twice.
        if element.find(list(_BLOCK_TAGS)) is not None:
            continue
        block = _normalise(element.get_text(" ", strip=True))
        if len(block) < _MIN_BLOCK_CHARS or block in seen:
            continue
        seen.add(block)
        blocks.append(block)
        if element.name in _HEADING_TAGS and len(headings) < MAX_HEADINGS:
            headings.append(block)

    text = "\n".join(blocks)
    truncated = len(text) > max_chars
    if truncated:
        clipped = text[:max_chars]
        # Cut at a boundary so the last line is not a fragment of a word.
        boundary = max(clipped.rfind("\n"), clipped.rfind(" "))
        text = clipped[:boundary] if boundary > max_chars // 2 else clipped

    return ExtractedContent(title=title, headings=headings, text=text, truncated=truncated)


def _is_same_origin(base: str, candidate: str) -> bool:
    """Same scheme family and identical host. The host is never allowed to change."""
    base_split, candidate_split = urlsplit(base), urlsplit(candidate)
    if candidate_split.scheme.lower() not in ("http", "https"):
        return False
    return (candidate_split.hostname or "").lower() == (base_split.hostname or "").lower()


def select_internal_links(
    html: str, *, base_url: str, limit: int, exclude: set[str] | None = None
) -> list[InternalLink]:
    """Choose up to ``limit`` same-origin pages worth fetching next.

    Deterministic: candidates are grouped by role, ordered within a role by path depth then
    path length then alphabetically, and taken one role at a time in priority order. The
    same HTML always produces the same selection.
    """
    if limit <= 0:
        return []

    excluded = {normalize_url(url) for url in (exclude or set())}
    best: dict[PageRole, list[tuple[tuple[int, int, str], InternalLink]]] = {}

    for anchor in _soup(html).find_all("a", href=True):
        href = str(anchor.get("href") or "").strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:", "data:")):
            continue

        resolved, _ = urldefrag(urljoin(base_url, href))
        if not _is_same_origin(base_url, resolved):
            continue

        path = urlsplit(resolved).path or "/"
        lowered = path.lower()
        if lowered in ("", "/"):
            continue
        if lowered.endswith(_EXCLUDED_SUFFIXES):
            continue
        if any(fragment in lowered for fragment in _EXCLUDED_FRAGMENTS):
            continue

        role = role_for_path(path)
        if role is None:
            continue

        canonical = normalize_url(resolved)
        if canonical in excluded:
            continue

        depth = len([segment for segment in path.split("/") if segment])
        sort_key = (depth, len(path), canonical)
        entry = InternalLink(
            url=canonical, role=role, anchor=_normalise(anchor.get_text(" ", strip=True))
        )
        bucket = best.setdefault(role, [])
        if all(existing.url != canonical for _, existing in bucket):
            bucket.append((sort_key, entry))

    chosen: list[InternalLink] = []
    for role, _ in _ROLE_PATTERNS:
        if len(chosen) >= limit:
            break
        bucket = best.get(role, [])
        if not bucket:
            continue
        # One page per role: a second pricing page adds far less than a first about page.
        chosen.append(min(bucket, key=lambda item: item[0])[1])
    return chosen
