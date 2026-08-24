"""Content fingerprints over a stage's authoritative inputs.

A derived artifact is only valid for the input it was derived from. Without a way to say
which input that was, "the report exists, so skip the stage" quietly becomes "render last
week's analysis over this week's evidence" - and nothing in the output says so.

Each fingerprint is a SHA-256 over a **canonical projection** of the artifacts a stage
reads: sorted, whitespace-free JSON of the fields that determine the stage's work, and
nothing else. Timestamps are excluded deliberately - a re-run that produces identical
evidence at a different second has not changed the input, and treating it as a change would
make resume useless.

The projections are deliberately shallow. They record *identity and shape* - which
companies, which claims, which statuses - not the prose. A model rewording a rationale does
not invalidate the memo built from it; a claim appearing or disappearing does.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from vc_scout.models.analysis import StartupAnalysis
from vc_scout.models.candidate import CandidateSet
from vc_scout.models.evidence import EvidenceDossier
from vc_scout.models.page import PageBundle
from vc_scout.models.recommendation import RecommendationResult

__all__ = [
    "FINGERPRINT_VERSION",
    "analysis_fingerprint",
    "candidates_fingerprint",
    "enrichment_fingerprint",
    "evidence_fingerprint",
    "fingerprint",
    "recommendation_fingerprint",
]

#: Bumped when a projection below changes shape. A fingerprint from an older version can
#: never match a newer one, which forces a rerun rather than a silent mismatch.
FINGERPRINT_VERSION = "fp1"

_DIGEST_LEN = 16


def fingerprint(kind: str, payload: Any) -> str:
    """A stable digest over ``payload``, tagged with ``kind`` and the projection version."""
    canonical = json.dumps(
        [FINGERPRINT_VERSION, kind, payload],
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return f"{FINGERPRINT_VERSION}:{hashlib.sha256(canonical.encode()).hexdigest()[:_DIGEST_LEN]}"


def candidates_fingerprint(candidate_set: CandidateSet) -> str:
    """What the sourcing stage decided: which companies, from which query and sources."""
    return fingerprint(
        "candidates",
        {
            "query": candidate_set.query,
            "limit": candidate_set.requested_limit,
            "candidates": sorted(
                [
                    candidate.company_id,
                    candidate.name,
                    candidate.website or "",
                    *sorted(candidate.source_ids),
                ]
                for candidate in candidate_set.candidates
            ),
        },
    )


def enrichment_fingerprint(bundles: list[PageBundle]) -> str:
    """What enrichment read: which pages, and the content hash of each."""
    return fingerprint(
        "enrichment",
        sorted(
            [
                bundle.company_id,
                bundle.status.value,
                *sorted(f"{page.source_id}:{page.content_sha256}" for page in bundle.pages),
            ]
            for bundle in bundles
        ),
    )


def evidence_fingerprint(dossiers: list[EvidenceDossier]) -> str:
    """What extraction established: which claims, unknowns and conflicts, per company."""
    return fingerprint(
        "evidence",
        sorted(
            [
                dossier.company_id,
                *sorted(claim.claim_id for claim in dossier.claims),
                *sorted(f"u:{unknown.question}" for unknown in dossier.unknowns),
                *sorted(f"c:{conflict.summary}" for conflict in dossier.conflicts),
            ]
            for dossier in dossiers
        ),
    )


def analysis_fingerprint(analyses: list[tuple[StartupAnalysis, RecommendationResult]]) -> str:
    """What analysis concluded: the scored shape and the binding call, per company.

    Prose is excluded on purpose. A reworded rationale is the same analysis for the purpose
    of deciding whether the memos below it are still the memos for this evidence.
    """
    return fingerprint(
        "analysis",
        sorted(
            [
                analysis.company_id,
                str(analysis.total_score),
                recommendation.decision.value,
                *sorted(
                    f"{component.component.value}:{component.score}:"
                    f"{component.assessment_status.value}"
                    for component in analysis.score_components
                ),
            ]
            for analysis, recommendation in analyses
        ),
    )


def recommendation_fingerprint(company_ids: list[str], template_version: str) -> str:
    """What the Markdown stage produced: which memos, under which template."""
    return fingerprint("recommendation", [template_version, sorted(company_ids)])
