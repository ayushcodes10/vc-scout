"""Small builders so tests state only what they are actually exercising."""

from __future__ import annotations

from vc_scout.models.analysis import ScoreComponent, StartupAnalysis
from vc_scout.models.enums import ClaimLabel, RubricDimension, SourceKind
from vc_scout.models.evidence import EvidenceClaim, EvidenceDossier
from vc_scout.models.source import SourceReference
from vc_scout.rubric import RUBRIC

COMPANY_ID = "acme-ops"


def source(url: str = "https://acme-ops.example/about") -> SourceReference:
    return SourceReference.create(url, kind=SourceKind.COMPANY_PAGE, title="About")


def claim(
    src: SourceReference,
    text: str = "Acme Ops says it reconciles invoices for plumbing contractors.",
    label: ClaimLabel = ClaimLabel.COMPANY_CLAIM,
    dimension: RubricDimension | None = RubricDimension.PAIN_ROI,
) -> EvidenceClaim:
    return EvidenceClaim.create(
        company_id=COMPANY_ID,
        claim=text,
        label=label,
        source_ids=[src.source_id],
        dimension=dimension,
    )


def dossier() -> EvidenceDossier:
    src = source()
    return EvidenceDossier(company_id=COMPANY_ID, claims=[claim(src)], sources=[src])


def analysis_scoring(points: int, **fields: object) -> StartupAnalysis:
    """An analysis totalling ``points``, spread over dimensions in rubric order.

    Every component it scores cites one evidence ID, so the resulting analysis is valid
    without the test having to spell out seven components.
    """
    remaining = points
    components: list[ScoreComponent] = []
    for spec in RUBRIC:
        awarded = min(spec.max_points, remaining)
        remaining -= awarded
        if awarded > 0:
            components.append(
                ScoreComponent.scored(
                    spec.key, awarded, evidence_ids=["ev-000000000001"], rationale="fixture"
                )
            )
    if remaining:
        raise ValueError(f"cannot distribute {points} points across the rubric")
    return StartupAnalysis.build(company_id=COMPANY_ID, components=components, **fields)


def analysis_full_coverage(fraction: float, **fields: object) -> StartupAnalysis:
    """An analysis where every dimension is scored, at ``fraction`` of its maximum.

    Lets a test vary the total score while holding evidence coverage fixed.
    """
    components = [
        ScoreComponent.scored(
            spec.key,
            max(1, round(spec.max_points * fraction)),
            evidence_ids=["ev-000000000001"],
            rationale="fixture",
        )
        for spec in RUBRIC
    ]
    return StartupAnalysis.build(company_id=COMPANY_ID, components=components, **fields)
