"""Validation of model-supplied analysis against the dossier it was given.

**This module is the analysis contract.** The provider-facing schema in
:mod:`vc_scout.llm.analysis_schema` exists only to be compiled into a decoding grammar, and
was deliberately compacted after the API rejected the earlier one as too large. Everything
that schema can no longer express is enforced here, where it always was: the seven exact
components, the configured rubric maxima, the assessment-status ceilings, the recomputed
total, reference integrity, grounding, market-size scrubbing, and the two-or-three
recommendation changers.

The model is an untrusted witness here too. Nothing it returns is written until it has been
checked against the evidence it actually saw:

* every cited claim ID and unknown reference exists in *this* candidate's dossier;
* all seven rubric dimensions appear exactly once, with the configured maxima;
* every score respects the ceiling its assessment status permits;
* no market-size figure appears that the evidence does not carry;
* exactly two or three recommendation changers are given.

The total is recomputed rather than accepted, and the research confidence and binding
recommendation are produced elsewhere - the model supplies neither.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from pydantic import ValidationError

from vc_scout.assessment_policy import GUARDED_QUANTITATIVE, quantitative_outcome_terms
from vc_scout.llm.analysis_schema import SECTION_KINDS, SINGULAR_KINDS, SectionKind
from vc_scout.models.analysis import (
    MAX_RECOMMENDATION_CHANGERS,
    MIN_RECOMMENDATION_CHANGERS,
    AnalysisSection,
    CompetitiveObservation,
    CorroboratedFinding,
    RiskItem,
    ScoreComponent,
    ThesisAssessment,
    ceiling_for,
)
from vc_scout.models.enums import (
    AssessmentStatus,
    LlmErrorCategory,
    Recommendation,
    RubricDimension,
    ThesisFit,
    VerificationStatus,
)
from vc_scout.models.evidence import EvidenceClaim, EvidenceDossier
from vc_scout.rubric import RUBRIC, max_points_for
from vc_scout.util.ids import unknown_id_for

__all__ = [
    "AnalysisValidationError",
    "DossierIndex",
    "SupportEvidence",
    "ValidatedAnalysis",
    "find_unsupported_market_numbers",
    "index_dossier",
    "validate_analysis",
]

#: Words that make a number a market-size assertion rather than an ordinary figure.
_MARKET_TERMS = re.compile(
    r"\b(?:tam|sam|som|addressable market|market size|market opportunity|"
    r"market is worth|industry is worth)\b",
    re.IGNORECASE,
)
#: A figure carrying a magnitude suffix. These are the ones that get invented.
_SCALED_FIGURE = re.compile(
    r"[$€£]\s?\d[\d,.]*\s*(?:k|m|bn|b|t|million|billion|trillion)?\b"
    r"|\b\d[\d,.]*\s*(?:million|billion|trillion)\b",
    re.IGNORECASE,
)
_SENTENCE = re.compile(r"[^.!?]+[.!?]?")


@dataclass(frozen=True, slots=True)
class DossierIndex:
    """The identifiers and text a model was allowed to cite, for one candidate."""

    claim_ids: frozenset[str]
    unknown_ids: frozenset[str]
    corpus: str

    @property
    def sorted_claim_ids(self) -> list[str]:
        return sorted(self.claim_ids)


def index_dossier(dossier: EvidenceDossier) -> DossierIndex:
    """Build the citable identifier sets and the evidence text corpus.

    Unknowns carry no identifier on disk, so one is derived from the question text. The same
    derivation is used when the dossier is rendered for the model, so a reference the model
    copies back always resolves - and one it invents never does.
    """
    corpus = " ".join(
        [
            *(claim.claim for claim in dossier.claims),
            *(excerpt.excerpt for claim in dossier.claims for excerpt in claim.excerpts),
            *(unknown.question for unknown in dossier.unknowns),
            *(conflict.summary for conflict in dossier.conflicts),
        ]
    ).lower()
    return DossierIndex(
        claim_ids=frozenset(claim.claim_id for claim in dossier.claims),
        unknown_ids=frozenset(
            unknown_id_for(dossier.company_id, unknown.question) for unknown in dossier.unknowns
        ),
        corpus=corpus,
    )


@dataclass(frozen=True, slots=True)
class SupportEvidence:
    """What a dossier can say about a `supported` rating, mechanically.

    Three facts, and nothing that requires reading meaning: whether every cited claim is
    the company talking about itself, whether any cited claim draws on a source the dossier
    records a conflict over, and which claims were cited at all.
    """

    company_authored_only: bool
    touches_conflict: bool
    cited: tuple[EvidenceClaim, ...]


def support_evidence(dossier: EvidenceDossier, claim_ids: list[str]) -> SupportEvidence:
    """Assemble the provenance facts behind one component's citations."""
    index = dossier.claim_index()
    cited = tuple(index[claim_id] for claim_id in claim_ids if claim_id in index)
    conflicted = {source_id for conflict in dossier.conflicts for source_id in conflict.source_ids}
    return SupportEvidence(
        company_authored_only=bool(cited)
        and all(claim.verification_status is VerificationStatus.COMPANY_CLAIM for claim in cited),
        touches_conflict=any(
            source_id in conflicted for claim in cited for source_id in claim.source_ids
        ),
        cited=cited,
    )


def check_supported_rating(
    dimension: RubricDimension,
    *,
    rationale: str,
    caveats: list[str],
    evidence: SupportEvidence,
) -> list[str]:
    """Reasons this dimension may not be rated `supported`, if any.

    Only the mechanically decidable ones. Whether a rationale genuinely follows from its
    evidence is a judgement, and a keyword heuristic that pretended otherwise would
    manufacture more false confidence than it prevented. What *is* decidable:

    1. a performance, scale or market figure asserted with nothing but the company's own
       word behind it - the claim is evidence of the claim, not of the result;
    2. a rating that rests on a source the dossier records a conflict over, with no caveat
       acknowledging it.

    Note what is deliberately absent: provenance alone never blocks `supported`, and the
    ``independently_supported`` label never grants it. Both were the point of the change.
    """
    reasons: list[str] = []
    if (
        dimension in GUARDED_QUANTITATIVE
        and evidence.company_authored_only
        and (offenders := quantitative_outcome_terms(rationale))
    ):
        reasons.append(
            f"asserts {' and '.join(offenders)} on company-authored evidence alone; "
            "a self-reported result is at most partially_supported until another "
            "voice corroborates it"
        )
    if evidence.touches_conflict and not caveats:
        reasons.append(
            "rests on a source the dossier records a conflict over; keep it contradicted, "
            "or record the conflict in caveats"
        )
    return reasons


class AnalysisValidationError(Exception):
    """The model's analysis could not be accepted. Carries per-issue detail for the retry."""

    def __init__(self, category: LlmErrorCategory, errors: list[str]) -> None:
        super().__init__("; ".join(errors[:5]))
        self.category = category
        self.errors = errors


@dataclass(slots=True)
class ValidatedAnalysis:
    """The validated parts of a model analysis, before confidence and policy are applied."""

    plain_language_product: str
    buyer: str | None
    workflow: str | None
    team_assessment: AnalysisSection
    product_assessment: AnalysisSection
    market_assessment: AnalysisSection
    thesis_assessment: ThesisAssessment
    competitive_observations: list[CompetitiveObservation]
    corroborated_findings: list[CorroboratedFinding]
    risks: list[RiskItem]
    open_questions: list[str]
    score_components: list[ScoreComponent]
    total_score: int
    model_suggested_recommendation: Recommendation | None
    recommendation_changers: list[str]
    identity_warnings: list[str]
    analysis_warnings: list[str]


def find_unsupported_market_numbers(text: str, corpus: str) -> list[str]:
    """Market-size figures in ``text`` that the evidence corpus does not carry.

    Two shapes are checked: any sentence that pairs a market term with a figure, and any
    standalone figure carrying a magnitude suffix. A figure that appears in the evidence is
    fine - the model is quoting. One that does not is invented, which is the single most
    damaging thing an investment memo can contain.
    """
    offenders: list[str] = []
    for sentence in _SENTENCE.findall(text):
        figures = [match.group(0).strip() for match in _SCALED_FIGURE.finditer(sentence)]
        if not figures:
            continue
        market_context = bool(_MARKET_TERMS.search(sentence))
        for figure in figures:
            normalised = figure.lower().replace(" ", "")
            if normalised in corpus.replace(" ", ""):
                continue
            if market_context or re.search(
                r"(?:million|billion|trillion|bn\b|[$€£])", figure, re.IGNORECASE
            ):
                offenders.append(f"{figure!r} in: {sentence.strip()[:120]}")
    return offenders


def _clean_ids(raw: Any) -> list[str]:
    """Normalise an identifier list: strings only, whitespace-stripped, first occurrence
    wins. Duplicate references are a formatting artifact, not a validation failure."""
    seen: dict[str, None] = {}
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, str) and item.strip():
                seen.setdefault(item.strip(), None)
    return list(seen)


def _check_ids(
    ids: list[str], index: DossierIndex, *, kind: str, label: str, errors: list[str]
) -> list[str]:
    known = index.claim_ids if kind == "evidence" else index.unknown_ids
    unknown = [value for value in ids if value not in known]
    if unknown:
        available = index.sorted_claim_ids if kind == "evidence" else sorted(index.unknown_ids)
        errors.append(
            f"{label}: {kind} reference(s) {unknown} do not exist in this candidate's "
            f"dossier. Valid values are: {', '.join(available) or '(none)'}"
        )
    return [value for value in ids if value in known]


def _partition_sections(raw: Any, errors: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Group the flat ``sections`` array by ``kind``.

    The provider schema carries one object shape for every grounded statement, because six
    distinct shapes is what made the compiled grammar too large. Splitting them back out is
    local work, and the per-kind rules are unchanged by it.
    """
    grouped: dict[str, list[dict[str, Any]]] = {kind: [] for kind in SECTION_KINDS}
    if not isinstance(raw, list):
        errors.append("sections: must be an array")
        return grouped

    for position, item in enumerate(raw):
        label = f"sections[{position}]"
        if not isinstance(item, dict):
            errors.append(f"{label}: must be an object")
            continue
        kind = item.get("kind")
        if kind not in grouped:
            errors.append(
                f"{label}.kind: {kind!r} is not valid. Valid values are: {', '.join(SECTION_KINDS)}"
            )
            continue
        grouped[str(kind)].append(item)

    for kind in SINGULAR_KINDS:
        if len(grouped[kind]) != 1:
            errors.append(
                f"sections: exactly one section of kind {kind!r} is required; "
                f"got {len(grouped[kind])}"
            )
    return grouped


def _section(
    raw: Any, index: DossierIndex, *, label: str, errors: list[str]
) -> AnalysisSection | None:
    if not isinstance(raw, dict):
        errors.append(f"{label}: must be an object")
        return None
    text = raw.get("text")
    if not isinstance(text, str) or not text.strip():
        errors.append(f"{label}.text: a non-empty assessment is required")
        return None
    claims = _check_ids(
        _clean_ids(raw.get("evidence_claim_ids")),
        index,
        kind="evidence",
        label=label,
        errors=errors,
    )
    unknowns = _check_ids(
        _clean_ids(raw.get("unknown_references")), index, kind="unknown", label=label, errors=errors
    )
    try:
        return AnalysisSection(
            text=" ".join(text.split()), evidence_claim_ids=claims, unknown_references=unknowns
        )
    except ValidationError as exc:
        errors.append(f"{label}: {exc.errors()[0].get('msg', 'invalid section')}")
        return None


def _score_components(
    raw: Any, index: DossierIndex, dossier: EvidenceDossier, errors: list[str]
) -> list[ScoreComponent]:
    if not isinstance(raw, list):
        errors.append("score_components: must be an array")
        return []

    components: list[ScoreComponent] = []
    seen: set[RubricDimension] = set()
    for position, item in enumerate(raw):
        label = f"score_components[{position}]"
        if not isinstance(item, dict):
            errors.append(f"{label}: must be an object")
            continue
        try:
            dimension = RubricDimension(str(item.get("component")))
        except ValueError:
            errors.append(
                f"{label}.component: {item.get('component')!r} is not a rubric dimension. "
                f"Valid values are: {', '.join(d.value for d in RubricDimension)}"
            )
            continue
        if dimension in seen:
            errors.append(f"{label}: {dimension.value} appears more than once")
            continue
        seen.add(dimension)

        try:
            status = AssessmentStatus(str(item.get("assessment_status")))
        except ValueError:
            errors.append(
                f"{label}.assessment_status: {item.get('assessment_status')!r} is not valid. "
                f"Valid values are: {', '.join(s.value for s in AssessmentStatus)}"
            )
            continue

        score = item.get("score")
        if not isinstance(score, int) or isinstance(score, bool):
            errors.append(f"{label}.score: an integer score is required")
            continue
        configured = max_points_for(dimension)
        ceiling = ceiling_for(dimension, status)
        if score < 0 or score > configured:
            errors.append(f"{label}.score: {score} is outside 0-{configured} for {dimension.value}")
            continue
        if score > ceiling:
            errors.append(
                f"{label}.score: {dimension.value} is {status.value} and may score at most "
                f"{ceiling} of {configured}; got {score}. Lower the score or raise the "
                "assessment status with evidence."
            )
            continue

        rationale = item.get("rationale")
        if not isinstance(rationale, str) or not rationale.strip():
            errors.append(f"{label}.rationale: a rationale is required")
            continue

        claims = _check_ids(
            _clean_ids(item.get("evidence_claim_ids")),
            index,
            kind="evidence",
            label=label,
            errors=errors,
        )
        unknowns = _check_ids(
            _clean_ids(item.get("unknown_references")),
            index,
            kind="unknown",
            label=label,
            errors=errors,
        )
        caveats = [c.strip() for c in item.get("caveats", []) if isinstance(c, str) and c.strip()]

        if status is AssessmentStatus.SUPPORTED and (
            reasons := check_supported_rating(
                dimension,
                rationale=rationale,
                caveats=caveats,
                evidence=support_evidence(dossier, claims),
            )
        ):
            errors.append(
                f"{label}: {dimension.value} may not be supported - "
                + "; ".join(reasons)
                + ". Lower the assessment status, or cite evidence that carries it."
            )
            continue

        try:
            components.append(
                ScoreComponent(
                    component=dimension,
                    score=score,
                    maximum=configured,
                    assessment_status=status,
                    rationale=" ".join(rationale.split()),
                    evidence_claim_ids=claims,
                    unknown_references=unknowns,
                    caveats=caveats,
                )
            )
        except ValidationError as exc:
            errors.append(f"{label}: {exc.errors()[0].get('msg', 'invalid component')}")

    if missing := sorted({spec.key for spec in RUBRIC} - seen):
        errors.append(
            f"score_components: missing rubric dimension(s) {[d.value for d in missing]}. "
            f"All {len(RUBRIC)} must appear exactly once."
        )
    return components


def validate_analysis(payload: dict[str, Any], *, dossier: EvidenceDossier) -> ValidatedAnalysis:
    """Turn a model payload into validated analysis parts, or raise with every problem.

    All issues are collected before raising, so the single retry sees the whole list.
    """
    index = index_dossier(dossier)
    errors: list[str] = []

    product = payload.get("plain_language_product")
    if not isinstance(product, str) or not product.strip():
        errors.append("plain_language_product: a plain-language description is required")
        product = ""

    grouped = _partition_sections(payload.get("sections"), errors)

    def one(kind: str) -> AnalysisSection | None:
        items = grouped[kind]
        if not items:
            return None
        return _section(items[0], index, label=f"sections[{kind}]", errors=errors)

    team = one(SectionKind.TEAM)
    prod = one(SectionKind.PRODUCT)
    market = one(SectionKind.MARKET)
    thesis = _thesis(payload.get("thesis_fit"), grouped[SectionKind.THESIS], index, errors)
    components = _score_components(payload.get("score_components"), index, dossier, errors)
    risks = _risks(grouped[SectionKind.RISK], index, errors)
    competitive = _competitive(grouped[SectionKind.COMPETITOR], index, errors)
    corroborated = _corroborated(grouped[SectionKind.CORROBORATED], index, errors)

    changers = [
        " ".join(item.split())
        for item in payload.get("recommendation_changers", [])
        if isinstance(item, str) and item.strip()
    ]
    if not MIN_RECOMMENDATION_CHANGERS <= len(changers) <= MAX_RECOMMENDATION_CHANGERS:
        errors.append(
            f"recommendation_changers: exactly {MIN_RECOMMENDATION_CHANGERS} or "
            f"{MAX_RECOMMENDATION_CHANGERS} items are required; got {len(changers)}"
        )

    suggested_raw = payload.get("model_suggested_recommendation")
    suggested: Recommendation | None = None
    if suggested_raw not in (None, ""):
        try:
            suggested = Recommendation(str(suggested_raw))
        except ValueError:
            errors.append(
                f"model_suggested_recommendation: {suggested_raw!r} is not valid. Valid "
                f"values are: {', '.join(r.value for r in Recommendation)}"
            )

    narrative = " ".join(
        [
            product,
            *(section.text for section in (team, prod, market) if section is not None),
            thesis.rationale if thesis else "",
            *(component.rationale for component in components),
            *(risk.text for risk in risks),
            *(observation.text for observation in competitive),
        ]
    )
    if offenders := find_unsupported_market_numbers(narrative, index.corpus):
        errors.append(
            "unsupported market-size figures that no supplied evidence carries: "
            + "; ".join(offenders[:3])
        )

    if errors:
        raise AnalysisValidationError(_category(errors), errors)

    assert team is not None and prod is not None and market is not None and thesis is not None
    return ValidatedAnalysis(
        plain_language_product=" ".join(product.split()),
        buyer=_optional_text(payload.get("buyer")),
        workflow=_optional_text(payload.get("workflow")),
        team_assessment=team,
        product_assessment=prod,
        market_assessment=market,
        thesis_assessment=thesis,
        competitive_observations=competitive,
        corroborated_findings=corroborated,
        risks=risks,
        open_questions=[
            " ".join(q.split())
            for q in payload.get("open_questions", [])
            if isinstance(q, str) and q.strip()
        ],
        score_components=components,
        total_score=sum(component.score for component in components),
        model_suggested_recommendation=suggested,
        recommendation_changers=changers,
        identity_warnings=_string_list(payload.get("identity_warnings")),
        analysis_warnings=_string_list(payload.get("analysis_warnings")),
    )


def _thesis(
    verdict_raw: Any, sections: list[dict[str, Any]], index: DossierIndex, errors: list[str]
) -> ThesisAssessment | None:
    """The verdict is a top-level enum; its rationale and grounding come from the section."""
    try:
        verdict = ThesisFit(str(verdict_raw))
    except ValueError:
        errors.append(
            f"thesis_fit: {verdict_raw!r} is not valid. Valid values are: "
            f"{', '.join(f.value for f in ThesisFit)}"
        )
        return None
    if not sections:
        return None

    raw = sections[0]
    rationale = raw.get("text")
    if not isinstance(rationale, str) or not rationale.strip():
        errors.append("sections[thesis].text: a rationale is required")
        return None
    claims = _check_ids(
        _clean_ids(raw.get("evidence_claim_ids")),
        index,
        kind="evidence",
        label="sections[thesis]",
        errors=errors,
    )
    unknowns = _check_ids(
        _clean_ids(raw.get("unknown_references")),
        index,
        kind="unknown",
        label="sections[thesis]",
        errors=errors,
    )
    try:
        return ThesisAssessment(
            verdict=verdict,
            rationale=" ".join(rationale.split()),
            evidence_claim_ids=claims,
            unknown_references=unknowns,
        )
    except ValidationError as exc:
        errors.append(f"thesis: {exc.errors()[0].get('msg', 'invalid')}")
        return None


def _risks(
    sections: list[dict[str, Any]], index: DossierIndex, errors: list[str]
) -> list[RiskItem]:
    risks: list[RiskItem] = []
    for position, item in enumerate(sections):
        label = f"sections[risk][{position}]"
        text = item.get("text")
        if not isinstance(text, str) or not text.strip():
            errors.append(f"{label}.text: a non-empty risk is required")
            continue
        claims = _check_ids(
            _clean_ids(item.get("evidence_claim_ids")),
            index,
            kind="evidence",
            label=label,
            errors=errors,
        )
        unknowns = _check_ids(
            _clean_ids(item.get("unknown_references")),
            index,
            kind="unknown",
            label=label,
            errors=errors,
        )
        try:
            risks.append(
                RiskItem(
                    text=" ".join(text.split()),
                    evidence_claim_ids=claims,
                    unknown_references=unknowns,
                )
            )
        except ValidationError:
            errors.append(
                f"{label}: a risk must cite evidence claim IDs, or name the recorded "
                "unknown it arises from"
            )
    return risks


def _competitive(
    sections: list[dict[str, Any]], index: DossierIndex, errors: list[str]
) -> list[CompetitiveObservation]:
    observations: list[CompetitiveObservation] = []
    for position, item in enumerate(sections):
        label = f"sections[competitor][{position}]"
        text = item.get("text")
        claims = _check_ids(
            _clean_ids(item.get("evidence_claim_ids")),
            index,
            kind="evidence",
            label=label,
            errors=errors,
        )
        if not isinstance(text, str) or not text.strip():
            errors.append(f"{label}.text: a non-empty observation is required")
            continue
        if not claims:
            errors.append(
                f"{label}: a competitive observation must cite evidence; a competitor may "
                "only be named when a supplied claim names it"
            )
            continue
        observations.append(
            CompetitiveObservation(text=" ".join(text.split()), evidence_claim_ids=claims)
        )
    return observations


def _corroborated(
    sections: list[dict[str, Any]], index: DossierIndex, errors: list[str]
) -> list[CorroboratedFinding]:
    findings: list[CorroboratedFinding] = []
    for position, item in enumerate(sections):
        label = f"sections[corroborated][{position}]"
        # The provider shape names this field `text`; the persisted record calls it `fact`.
        fact = item.get("text")
        claims = _check_ids(
            _clean_ids(item.get("evidence_claim_ids")),
            index,
            kind="evidence",
            label=label,
            errors=errors,
        )
        if not isinstance(fact, str) or not fact.strip():
            errors.append(f"{label}.text: a non-empty corroborated fact is required")
            continue
        if not claims:
            errors.append(f"{label}: a corroborated finding must cite the claims behind it")
            continue
        findings.append(CorroboratedFinding(fact=" ".join(fact.split()), evidence_claim_ids=claims))
    return findings


def _optional_text(value: Any) -> str | None:
    return " ".join(value.split()) if isinstance(value, str) and value.strip() else None


def _string_list(raw: Any) -> list[str]:
    return [
        " ".join(item.split())
        for item in (raw if isinstance(raw, list) else [])
        if isinstance(item, str) and item.strip()
    ]


def _category(errors: list[str]) -> LlmErrorCategory:
    """The most specific category describing this batch of errors."""
    joined = " ".join(errors)
    if "do not exist in this candidate's dossier" in joined:
        return LlmErrorCategory.UNKNOWN_EVIDENCE_REFERENCE
    if "may score at most" in joined or "is outside 0-" in joined:
        return LlmErrorCategory.INVALID_SCORE
    if "model_suggested_recommendation" in joined:
        return LlmErrorCategory.INVALID_RECOMMENDATION
    return LlmErrorCategory.SCHEMA_VALIDATION_FAILED
