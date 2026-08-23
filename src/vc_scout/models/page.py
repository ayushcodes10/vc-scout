"""Enrichment output: readable text recovered from a fetched page."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, computed_field

from vc_scout.models.base import ArtifactModel, RecordModel
from vc_scout.models.enums import EnrichmentStatus, FetchFailure, PageRole
from vc_scout.models.source import SourceReference
from vc_scout.util.ids import COMPANY_ID_PATTERN

__all__ = ["ExtractedPage", "PageBundle", "PageFailure"]


class ExtractedPage(RecordModel):
    """Main-content text extracted from one fetched page.

    ``text`` is what the evidence-extraction prompt will be allowed to see. It is untrusted
    input: it comes from a third-party website and is treated as data, never as
    instructions.

    ``url`` is what was requested and ``final_url`` is where the request landed. They differ
    whenever a redirect was followed, and both are kept so a citation can name the page that
    was actually read.
    """

    company_id: str = Field(pattern=COMPANY_ID_PATTERN.pattern)
    source_id: str = Field(pattern=r"^src-[0-9a-f]{12}$")
    url: str
    final_url: str
    text: str
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    role: PageRole | None = None
    title: str | None = None
    headings: list[str] = Field(default_factory=list)
    http_status: int | None = None
    content_type: str | None = None
    fetched_at: datetime | None = None
    extractor: str | None = None
    #: True when ``text`` was cut at the per-page cap. The cap, not the site, ended it.
    truncated: bool = False
    #: True when the response body itself hit the byte ceiling mid-stream.
    body_truncated: bool = False

    @computed_field  # type: ignore[prop-decorator]
    @property
    def char_count(self) -> int:
        return len(self.text)


class PageFailure(RecordModel):
    """One page that could not be turned into text, and why."""

    url: str
    category: FetchFailure
    role: PageRole | None = None
    detail: str | None = None
    http_status: int | None = None


class PageBundle(ArtifactModel):
    """The persisted ``extracted/<company_id>.json`` document.

    Written for every candidate, including candidates whose site could not be read at all.
    An empty bundle is a fact about the research, and downstream stages need to see it.
    """

    company_id: str = Field(pattern=COMPANY_ID_PATTERN.pattern)
    status: EnrichmentStatus = EnrichmentStatus.FAILED
    pages: list[ExtractedPage] = Field(default_factory=list)
    sources: list[SourceReference] = Field(default_factory=list)
    failures: list[PageFailure] = Field(default_factory=list)
    generated_at: datetime | None = None
    warnings: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_chars(self) -> int:
        return sum(page.char_count for page in self.pages)

    def source_index(self) -> dict[str, SourceReference]:
        return {source.source_id: source for source in self.sources}
