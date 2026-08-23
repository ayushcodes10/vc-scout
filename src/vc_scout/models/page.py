"""Enrichment output: readable text recovered from a fetched page."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, computed_field

from vc_scout.models.base import ArtifactModel, RecordModel
from vc_scout.models.source import SourceReference
from vc_scout.util.ids import COMPANY_ID_PATTERN

__all__ = ["ExtractedPage", "PageBundle"]


class ExtractedPage(RecordModel):
    """Main-content text extracted from one source document.

    ``text`` is what the evidence-extraction prompt is allowed to see. It is untrusted
    input: it originates from a third-party website and is treated as data, never as
    instructions.
    """

    company_id: str = Field(pattern=COMPANY_ID_PATTERN.pattern)
    source_id: str = Field(pattern=r"^src-[0-9a-f]{12}$")
    url: str
    text: str

    title: str | None = None
    extractor: str | None = None
    extracted_at: datetime | None = None
    truncated: bool = False
    http_status: int | None = None
    content_sha256: str | None = None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def char_count(self) -> int:
        return len(self.text)


class PageBundle(ArtifactModel):
    """The persisted ``extracted/<company_id>.json`` document."""

    company_id: str = Field(pattern=COMPANY_ID_PATTERN.pattern)
    pages: list[ExtractedPage] = Field(default_factory=list)
    sources: list[SourceReference] = Field(default_factory=list)
    generated_at: datetime | None = None
    warnings: list[str] = Field(default_factory=list)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def total_chars(self) -> int:
        return sum(page.char_count for page in self.pages)
