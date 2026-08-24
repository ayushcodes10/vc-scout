"""Safe Markdown primitives.

Every string that reaches a memo has one of two provenances, and they are treated
differently:

* **Renderer-authored** - headings, table scaffolding, labels, and the policy's own
  rationale sentences, which :mod:`vc_scout.policy` assembles from enum values and
  integers. These are emitted verbatim.
* **Source-controlled** - company names, page titles, model narrative and page excerpts.
  All of it originated on a third-party website, so all of it is untrusted and passes
  through :func:`text` before it can appear in a document.

The neutralisation rule is: untrusted text may contribute *words*, never *structure*. It
cannot open a heading, a table cell, a code fence, an HTML block, a link or an image, and
it cannot span lines - every run of whitespace, newlines included, collapses to one space
before escaping, so there is no position in a rendered memo where untrusted text begins a
block.

Links follow a second rule: **the memo never renders a link whose text differs from its
target.** A source URL is emitted as an autolink, so a reader always sees where a citation
actually points, and a deceptive label is not expressible.
"""

from __future__ import annotations

import re
import unicodedata

from vc_scout.models.source import is_safe_url

__all__ = [
    "ELLIPSIS",
    "autolink",
    "cell",
    "internal_link",
    "join_markers",
    "text",
    "truncate_words",
    "verbatim",
]

ELLIPSIS = "…"

#: Characters that carry Markdown or HTML structure wherever they appear inline.
#: ``(`` and ``)`` are deliberately absent: with ``[`` and ``]`` escaped, a bare
#: parenthesis cannot form a link, and escaping ordinary prose punctuation makes the
#: rendered memo harder to read for no security gain.
_INLINE_SPECIALS = "\\`*_[]<>|~"
_INLINE_ESCAPE = {ord(char): f"\\{char}" for char in _INLINE_SPECIALS}

#: Characters that only carry structure at the start of a line - headings, blockquotes,
#: list markers, setext underlines. Whitespace collapsing means untrusted text can only
#: ever occupy a line's start when the renderer puts it there, but escaping the first
#: character is cheap and removes the need to reason about that per template.
_LEADING_SPECIALS = frozenset("#>+-=:")

_ORDERED_LIST_START = re.compile(r"^(\d{1,9})([.)])")
_WHITESPACE = re.compile(r"\s+")
#: Anything a URL must not contain if it is to survive as a single Markdown autolink.
_URL_FORBIDDEN = re.compile(r"[\s<>()\[\]\\`\"']")


def _strip_controls(value: str) -> str:
    """Drop control and format characters, which render invisibly or reorder text.

    Bidirectional overrides in particular can make a rendered line read as something other
    than its source, which is exactly the class of trick this module exists to remove.
    Whitespace controls - newlines and tabs - become spaces rather than disappearing, so
    that removing them cannot silently weld two words together.
    """
    out: list[str] = []
    for char in value:
        if char.isspace():
            out.append(" ")
        elif unicodedata.category(char) not in {"Cc", "Cf"}:
            out.append(char)
    return "".join(out)


def text(value: str | None, *, empty: str = "") -> str:
    """Neutralise untrusted text so it can contribute words but never structure."""
    if value is None:
        return empty
    collapsed = _WHITESPACE.sub(" ", _strip_controls(value)).strip()
    if not collapsed:
        return empty
    escaped = collapsed.translate(_INLINE_ESCAPE)
    if escaped[0] in _LEADING_SPECIALS:
        escaped = "\\" + escaped
    elif match := _ORDERED_LIST_START.match(escaped):
        escaped = f"{match.group(1)}\\{match.group(2)}{escaped[match.end() :]}"
    return escaped


def verbatim(value: str) -> str:
    """Emit renderer-authored text unchanged, apart from collapsing whitespace.

    Used only for strings this codebase assembled itself - the policy rationale above all,
    which must appear in the memo exactly as the recommendation artifact records it.
    """
    return _WHITESPACE.sub(" ", value).strip()


def truncate_words(value: str, limit: int) -> str:
    """Cut ``value`` to at most ``limit`` words, marking the cut.

    Applied before escaping so a truncation can never split an escape sequence.
    """
    words = value.split()
    if len(words) <= limit:
        return value
    return " ".join(words[:limit]) + ELLIPSIS


def cell(value: str | None, limit: int = 28, *, empty: str = "-") -> str:
    """Neutralised text short enough that a table row stays readable."""
    if value is None:
        return empty
    return text(truncate_words(value, limit), empty=empty)


def autolink(url: str) -> str:
    """Render ``url`` as an autolink, or as inert text when it is not renderable.

    Only absolute ``http``/``https`` URLs become links. ``javascript:``, ``data:``,
    ``file:`` and every other scheme are refused, and so is any URL carrying characters
    that would break out of the autolink - such a URL is shown escaped, as text, so the
    citation is still auditable without being clickable.
    """
    stripped = (url or "").strip()
    if not stripped:
        return "no URL recorded"
    if not is_safe_url(stripped) or _URL_FORBIDDEN.search(stripped):
        return f"{text(stripped)} (link withheld: unsupported URL)"
    return f"<{stripped}>"


def internal_link(label: str, target: str) -> str:
    """A link to another file in this run.

    ``target`` is built by :class:`~vc_scout.store.RunStore` from a validated company ID,
    so it is renderer-authored; ``label`` is not, and is neutralised.
    """
    if _URL_FORBIDDEN.search(target):
        raise ValueError(f"refusing to render an unusable relative link target {target!r}")
    return f"[{text(label, empty=target)}]({target})"


def join_markers(markers: list[str]) -> str:
    """Render source markers as ``[S1] [S2]``, in the order given."""
    return " ".join(f"[{marker}]" for marker in markers)
