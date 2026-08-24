"""Small builders so tests state only what they are actually exercising."""

from __future__ import annotations

from vc_scout.models.enums import (
    EvidenceCategory,
    InferenceStatus,
    SourceKind,
    VerificationStatus,
)
from vc_scout.models.evidence import EvidenceClaim, EvidenceDossier, SupportingExcerpt
from vc_scout.models.source import SourceReference

COMPANY_ID = "acme-ops"


def source(url: str = "https://acme-ops.example/about") -> SourceReference:
    return SourceReference.create(url, kind=SourceKind.COMPANY_PAGE, title="About")


def claim(
    src: SourceReference,
    text: str = "Acme Ops says it reconciles invoices for plumbing contractors.",
    verification: VerificationStatus = VerificationStatus.COMPANY_CLAIM,
    category: EvidenceCategory = EvidenceCategory.PRODUCT,
    excerpt: str = "reconciles invoices for plumbing contractors",
    inference: InferenceStatus = InferenceStatus.EXPLICIT,
) -> EvidenceClaim:
    return EvidenceClaim.create(
        company_id=COMPANY_ID,
        category=category,
        claim=text,
        excerpts=[SupportingExcerpt(source_id=src.source_id, excerpt=excerpt)],
        verification_status=verification,
        inference_status=inference,
    )


def dossier() -> EvidenceDossier:
    src = source()
    return EvidenceDossier(company_id=COMPANY_ID, claims=[claim(src)], sources=[src])
