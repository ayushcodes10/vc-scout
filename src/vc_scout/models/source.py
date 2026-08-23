"""Sources and traction signals - the bottom of the citation chain.

Nothing in a memo is allowed to exist without terminating in a :class:`SourceReference`
that a partner can click.
"""

from __future__ import annotations

from datetime import datetime
from urllib.parse import urlsplit

from pydantic import Field, field_validator

from vc_scout.models.base import RecordModel
from vc_scout.models.enums import ClaimLabel, SourceKind, TractionKind
from vc_scout.util.ids import normalize_url, registrable_domain, source_id_for

__all__ = ["SourceReference", "TractionSignal", "is_safe_url"]

_ALLOWED_SCHEMES = frozenset({"http", "https"})


def is_safe_url(url: str) -> bool:
    """True when ``url`` is an absolute http(s) URL with a host.

    Used both to reject unusable sources at ingest and, later, to decide whether a
    citation may be rendered as a live anchor in the generated site.
    """
    try:
        split = urlsplit(url.strip())
    except ValueError:
        return False
    return split.scheme.lower() in _ALLOWED_SCHEMES and bool(split.hostname)


class SourceReference(RecordModel):
    """A retrievable public document.

    ``source_id`` is derived from the normalised URL, so the same page always carries the
    same ID regardless of when or how it was discovered.
    """

    source_id: str = Field(pattern=r"^src-[0-9a-f]{12}$")
    url: str
    domain: str
    kind: SourceKind = SourceKind.OTHER

    # Everything below is unknown until observed, and stays None rather than being guessed.
    title: str | None = None
    retrieved_at: datetime | None = None
    published_at: datetime | None = None
    hn_object_id: str | None = None
    hn_points: int | None = Field(default=None, ge=0)
    hn_num_comments: int | None = Field(default=None, ge=0)

    @field_validator("url")
    @classmethod
    def _url_must_be_http(cls, value: str) -> str:
        if not is_safe_url(value):
            raise ValueError(f"source url must be an absolute http(s) URL, got {value!r}")
        return value

    @classmethod
    def create(
        cls,
        url: str,
        *,
        kind: SourceKind = SourceKind.OTHER,
        title: str | None = None,
        retrieved_at: datetime | None = None,
        published_at: datetime | None = None,
        hn_object_id: str | None = None,
        hn_points: int | None = None,
        hn_num_comments: int | None = None,
    ) -> SourceReference:
        """Build a reference, deriving ``source_id`` and ``domain`` from ``url``."""
        if not is_safe_url(url):
            raise ValueError(f"source url must be an absolute http(s) URL, got {url!r}")
        canonical = normalize_url(url)
        return cls(
            source_id=source_id_for(canonical),
            url=canonical,
            domain=registrable_domain(canonical),
            kind=kind,
            title=title,
            retrieved_at=retrieved_at,
            published_at=published_at,
            hn_object_id=hn_object_id,
            hn_points=hn_points,
            hn_num_comments=hn_num_comments,
        )


class TractionSignal(RecordModel):
    """An observed, source-backed indicator of traction.

    Kept structured and separate from narrative evidence so that freshness and magnitude
    can be reasoned about deterministically. Like every claim, it must cite sources.
    """

    kind: TractionKind
    value: str
    label: ClaimLabel
    source_ids: list[str] = Field(min_length=1)

    numeric_value: float | None = None
    unit: str | None = None
    observed_at: datetime | None = None

    @field_validator("source_ids")
    @classmethod
    def _source_ids_unique(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("source_ids must not contain duplicates")
        return value
