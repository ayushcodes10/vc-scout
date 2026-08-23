"""Builders for evidence-stage tests.

Everything here is offline. The provider is always :class:`FakeProvider` or a scripted
payload; no test in this suite is capable of reaching a network or reading a credential.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from vc_scout.models.candidate import Candidate, CandidateSet
from vc_scout.models.enums import EnrichmentStatus, PageRole, SourceKind
from vc_scout.models.page import ExtractedPage, PageBundle
from vc_scout.models.source import SourceReference
from vc_scout.store import RunStore

__all__ = [
    "HOMEPAGE_TEXT",
    "NOW",
    "claim_payload",
    "seed_run",
    "source_ids",
]

NOW = datetime(2026, 8, 25, 10, 0, tzinfo=UTC)

HOMEPAGE_TEXT = (
    "Acme Ops is an AI agent that reconciles invoices for plumbing contractors.\n"
    "Plans start at 49 dollars per month with no setup fee.\n"
    "We connect to QuickBooks and Xero so nobody rekeys an invoice twice.\n"
    "Built by two engineers who ran a field service business for six years."
)

PRICING_TEXT = (
    "Simple pricing. Starter is 49 dollars per month for up to 200 invoices.\n"
    "Growth is 199 dollars per month and adds unlimited invoices and two seats."
)


def seed_run(
    store: RunStore,
    *,
    company_id: str = "acme-ops",
    with_pages: bool = True,
    page_text: str = HOMEPAGE_TEXT,
    extra_candidates: int = 0,
) -> CandidateSet:
    """Write the candidates and page bundles the evidence stage reads."""
    store.ensure_root()
    candidates: list[Candidate] = []
    sources: list[SourceReference] = []

    for index, cid in enumerate([company_id, *(f"co-{i}" for i in range(extra_candidates))]):
        hn = SourceReference.create(
            f"https://news.ycombinator.com/item?id=90{index}",
            kind=SourceKind.HN_STORY,
            title=f"Show HN: {cid} - AI agent for invoicing",
            hn_points=42,
            hn_num_comments=17,
            published_at=NOW,
        )
        sources.append(hn)
        candidates.append(
            Candidate(
                company_id=cid,
                name=cid.replace("-", " ").title(),
                source_ids=[hn.source_id],
                one_liner="AI agent for invoicing",
                website=f"https://{cid}.example/",
            )
        )
        if with_pages:
            _write_bundle(store, cid, page_text)
        else:
            store.write_pages(
                PageBundle(company_id=cid, status=EnrichmentStatus.FAILED, generated_at=NOW)
            )

    bundle = CandidateSet(
        run_id=store.run_id,
        query="AI agents for SMB operations",
        candidates=candidates,
        sources=sources,
        generated_at=NOW,
    )
    store.write_candidates(bundle)
    return bundle


def _write_bundle(store: RunStore, company_id: str, page_text: str) -> None:
    home = SourceReference.create(f"https://{company_id}.example/", kind=SourceKind.COMPANY_PAGE)
    pricing = SourceReference.create(
        f"https://{company_id}.example/pricing", kind=SourceKind.COMPANY_PAGE
    )
    store.write_pages(
        PageBundle(
            company_id=company_id,
            status=EnrichmentStatus.SUCCESS,
            generated_at=NOW,
            sources=[home, pricing],
            pages=[
                ExtractedPage(
                    company_id=company_id,
                    source_id=home.source_id,
                    url=home.url,
                    final_url=home.url,
                    text=page_text,
                    content_sha256="a" * 64,
                    role=PageRole.HOMEPAGE,
                    title="Acme Ops",
                    fetched_at=NOW,
                ),
                ExtractedPage(
                    company_id=company_id,
                    source_id=pricing.source_id,
                    url=pricing.url,
                    final_url=pricing.url,
                    text=PRICING_TEXT,
                    content_sha256="b" * 64,
                    role=PageRole.PRICING,
                    title="Pricing",
                    fetched_at=NOW,
                ),
            ],
        )
    )


def source_ids(store: RunStore, company_id: str = "acme-ops") -> dict[str, str]:
    """Map a readable label to the stable source_id the stage will supply."""
    candidates = store.read_candidates()
    candidate = next(c for c in candidates.candidates if c.company_id == company_id)
    bundle = store.read_pages(company_id)
    ids = {"hn": candidate.source_ids[0]}
    for page in bundle.pages:
        ids[page.role.value if page.role else "page"] = page.source_id
    return ids


def claim_payload(
    source_id: str,
    *,
    excerpt: str = "reconciles invoices for plumbing contractors",
    claim: str = "The company describes itself as an AI agent that reconciles invoices.",
    category: str = "product",
    verification: str = "company_claim",
    inference: str = "explicit",
    extra_excerpts: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    """One claim as a model would emit it - with no claim_id, which is derived later."""
    return {
        "category": category,
        "claim": claim,
        "excerpts": [{"source_id": source_id, "excerpt": excerpt}, *(extra_excerpts or [])],
        "verification_status": verification,
        "inference_status": inference,
    }
