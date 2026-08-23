"""Validation of model-supplied evidence against the material actually supplied.

The model is treated as an untrusted witness. Nothing it returns is written to an artifact
until it has been checked against the exact sources it was given:

* every cited ``source_id`` was supplied for *this* candidate;
* every claim carries at least one supporting excerpt;
* every excerpt is present in the text of the source it is attached to;
* ``independently_supported`` is backed by genuinely separate sources;
* claim identifiers are recomputed from claim content, never taken from the model.

A claim that fails is rejected with a specific error, and those errors are what the single
retry sends back. Nothing is silently repaired.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pydantic import ValidationError

from vc_scout.models.enums import (
    EvidenceCategory,
    InferenceStatus,
    LlmErrorCategory,
    VerificationStatus,
)
from vc_scout.models.evidence import (
    MAX_EXCERPT_CHARS,
    MIN_EXCERPT_CHARS,
    ConfidenceInputs,
    EvidenceClaim,
    EvidenceConflict,
    EvidenceDossier,
    EvidenceUnknown,
    SourceCoverage,
    SupportingExcerpt,
)
from vc_scout.models.source import SourceReference

__all__ = [
    "EvidenceValidationError",
    "closest_span",
    "SuppliedSource",
    "ValidationOutcome",
    "normalize_for_match",
    "validate_evidence",
]

#: Every Unicode whitespace character, including the non-breaking space that HTML is full
#: of. Collapsing these is one of the two transformations applied before matching an
#: excerpt against its source - see docs/DECISIONS.md D25.
_WHITESPACE = re.compile(r"\s+", re.UNICODE)

#: A closed set of typographic punctuation folded to its ASCII equivalent. Exactly these
#: seven characters, and no others.
#:
#: These are rendering variants of the same punctuation mark, not different words. A page
#: that writes ``we're`` with U+2019 and a quotation that writes it with U+0027 are quoting
#: the same sentence, and rejecting that cost a fully supported dossier on the first live
#: run. Folding them does not permit paraphrasing: every letter, digit, word and word order
#: must still match exactly, and no other character is touched.
_PUNCTUATION_FOLD = str.maketrans(
    {
        "\u2018": "'",  # LEFT SINGLE QUOTATION MARK
        "\u2019": "'",  # RIGHT SINGLE QUOTATION MARK
        "\u201c": '"',  # LEFT DOUBLE QUOTATION MARK
        "\u201d": '"',  # RIGHT DOUBLE QUOTATION MARK
        "\u2013": "-",  # EN DASH
        "\u2014": "-",  # EM DASH
        "\u2026": "...",  # HORIZONTAL ELLIPSIS
    }
)

#: Bounds on the diagnostic span returned when an excerpt does not match. Long enough to
#: show the difference, short enough that it cannot become a second copy of the page.
_SPAN_MAX_CHARS = 160
#: An excerpt whose opening characters match nothing is not a near miss; reporting a span
#: for it would point at arbitrary text.
_SPAN_MIN_ANCHOR = 16
#: Context shown before the anchor, so a difference at the very start is visible.
_SPAN_LEAD_CHARS = 12


def normalize_for_match(text: str) -> str:
    """Normalised text, for comparing an excerpt against its source.

    Three transformations, in order, and no others:

    1. Unicode NFKC, so non-breaking and narrow spaces behave like ordinary ones;
    2. the closed punctuation fold in :data:`_PUNCTUATION_FOLD` - seven typographic
       characters mapped to their ASCII equivalents;
    3. whitespace collapse.

    Case, wording and word order are untouched, because an excerpt is a quotation. A
    paraphrase, a reordering or a change of case still fails to match.
    """
    folded = unicodedata.normalize("NFKC", text).translate(_PUNCTUATION_FOLD)
    return _WHITESPACE.sub(" ", folded).strip()


def closest_span(excerpt: str, source_text: str) -> str | None:
    """A short span of ``source_text`` near where ``excerpt`` starts to diverge.

    Purely diagnostic. It is never accepted as a match and never widens what validation
    allows - it exists so a retry can see the difference rather than guess at it, which is
    what defeated the retry on the first live run.

    Deterministic: it finds the longest leading run of the excerpt that does occur in the
    source - by binary search, since that property is monotonic - and returns a bounded
    window around it. Returns ``None`` when no leading run is long enough to be a genuine
    near miss, in which case the caller keeps the generic message.

    Both arguments must already be normalised, and ``source_text`` must be the text of the
    source the excerpt was attached to - never another source, and never another candidate.
    """
    if len(excerpt) < _SPAN_MIN_ANCHOR or excerpt[:_SPAN_MIN_ANCHOR] not in source_text:
        return None

    low, high = _SPAN_MIN_ANCHOR, len(excerpt)
    while low < high:
        middle = (low + high + 1) // 2
        if excerpt[:middle] in source_text:
            low = middle
        else:
            high = middle - 1

    index = source_text.find(excerpt[:low])
    start = max(0, index - _SPAN_LEAD_CHARS)
    return source_text[start : start + _SPAN_MAX_CHARS].strip() or None


@dataclass(frozen=True, slots=True)
class SuppliedSource:
    """One source as it was handed to the model, with the exact text it could quote."""

    reference: SourceReference
    text: str
    role: str | None = None
    truncated: bool = False

    @property
    def source_id(self) -> str:
        return self.reference.source_id


class EvidenceValidationError(Exception):
    """The model's output could not be accepted. Carries per-issue detail for the retry."""

    def __init__(self, category: LlmErrorCategory, errors: list[str]) -> None:
        super().__init__("; ".join(errors[:5]))
        self.category = category
        self.errors = errors


@dataclass(slots=True)
class ValidationOutcome:
    """A validated dossier, plus anything that was noted along the way."""

    dossier: EvidenceDossier
    warnings: list[str] = field(default_factory=list)


def _as_list(payload: dict[str, Any], key: str) -> list[Any]:
    value = payload.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise EvidenceValidationError(
            LlmErrorCategory.SCHEMA_VALIDATION_FAILED, [f"{key!r} must be an array"]
        )
    return value


def _enum(value: Any, enum_type: type, field_name: str, errors: list[str], index: int) -> Any:
    try:
        return enum_type(value)
    except ValueError:
        allowed = ", ".join(member.value for member in enum_type)  # type: ignore[attr-defined]
        errors.append(f"claims[{index}].{field_name}: {value!r} is not one of: {allowed}")
        return None


def _validate_excerpts(
    raw: Any,
    *,
    supplied: dict[str, SuppliedSource],
    normalized: dict[str, str],
    errors: list[str],
    label: str,
) -> list[SupportingExcerpt]:
    """Check each excerpt against the specific source it claims to come from."""
    if not isinstance(raw, list) or not raw:
        errors.append(f"{label}: at least one supporting excerpt is required")
        return []

    checked: list[SupportingExcerpt] = []
    for position, item in enumerate(raw):
        where = f"{label}.excerpts[{position}]"
        if not isinstance(item, dict):
            errors.append(f"{where}: must be an object with source_id and excerpt")
            continue

        source_id = item.get("source_id")
        excerpt = item.get("excerpt")
        if not isinstance(source_id, str) or source_id not in supplied:
            errors.append(
                f"{where}: source_id {source_id!r} was not supplied for this candidate. "
                f"Valid source_ids are: {', '.join(sorted(supplied))}"
            )
            continue
        if not isinstance(excerpt, str) or not excerpt.strip():
            errors.append(f"{where}: the supporting excerpt is empty")
            continue

        cleaned = normalize_for_match(excerpt)
        if len(cleaned) < MIN_EXCERPT_CHARS:
            errors.append(
                f"{where}: excerpt is {len(cleaned)} characters, "
                f"shorter than the {MIN_EXCERPT_CHARS}-character minimum"
            )
            continue
        if len(cleaned) > MAX_EXCERPT_CHARS:
            errors.append(
                f"{where}: excerpt is {len(cleaned)} characters, longer than the "
                f"{MAX_EXCERPT_CHARS}-character maximum. Quote a sentence, not a passage."
            )
            continue
        if cleaned not in normalized[source_id]:
            found_in = [sid for sid, text in normalized.items() if cleaned in text]
            if found_in:
                hint = (
                    f" It does appear in {found_in[0]}; attach an excerpt to the source it "
                    "came from."
                )
            elif (span := closest_span(cleaned, normalized[source_id])) is not None:
                # Show the retry what the source actually says, so a one-character
                # difference is visible rather than something to guess at.
                hint = f' The closest text in that source is: "{span}"'
            else:
                hint = " Copy the excerpt verbatim from the supplied text."
            errors.append(f"{where}: the excerpt does not appear in the text of {source_id}.{hint}")
            continue
        checked.append(SupportingExcerpt(source_id=source_id, excerpt=cleaned))
    return checked


def _validate_claims(
    raw_claims: list[Any],
    *,
    company_id: str,
    supplied: dict[str, SuppliedSource],
    normalized: dict[str, str],
    errors: list[str],
) -> list[EvidenceClaim]:
    claims: list[EvidenceClaim] = []
    seen_ids: set[str] = set()

    for index, raw in enumerate(raw_claims):
        label = f"claims[{index}]"
        if not isinstance(raw, dict):
            errors.append(f"{label}: must be an object")
            continue

        claim_text = raw.get("claim")
        if not isinstance(claim_text, str) or not claim_text.strip():
            errors.append(f"{label}.claim: a non-empty claim is required")
            continue

        category = _enum(raw.get("category"), EvidenceCategory, "category", errors, index)
        verification = _enum(
            raw.get("verification_status"), VerificationStatus, "verification_status", errors, index
        )
        inference = _enum(
            raw.get("inference_status"), InferenceStatus, "inference_status", errors, index
        )
        excerpts = _validate_excerpts(
            raw.get("excerpts"),
            supplied=supplied,
            normalized=normalized,
            errors=errors,
            label=label,
        )
        if category is None or verification is None or inference is None or not excerpts:
            continue

        source_ids: list[str] = []
        for excerpt in excerpts:
            if excerpt.source_id not in source_ids:
                source_ids.append(excerpt.source_id)

        if verification is VerificationStatus.INDEPENDENTLY_SUPPORTED and len(source_ids) < 2:
            errors.append(
                f"{label}.verification_status: independently_supported requires at least two "
                f"separate sources but only {len(source_ids)} is cited. Use company_claim or "
                "community_signal instead."
            )
            continue

        caveat = raw.get("caveat")
        try:
            claim = EvidenceClaim.create(
                company_id=company_id,
                category=category,
                claim=" ".join(claim_text.split()),
                excerpts=excerpts,
                verification_status=verification,
                inference_status=inference,
                caveat=caveat.strip() if isinstance(caveat, str) and caveat.strip() else None,
                source_ids=source_ids,
            )
        except ValidationError as exc:
            errors.append(f"{label}: {exc.errors()[0].get('msg', 'invalid claim')}")
            continue

        # Identity is derived from content, so two identical claims collide by design.
        if claim.claim_id in seen_ids:
            errors.append(
                f"{label}: duplicates an earlier claim with the same text and sources. "
                "Merge them or make them distinct."
            )
            continue
        seen_ids.add(claim.claim_id)
        claims.append(claim)
    return claims


def _validate_unknowns(raw_unknowns: list[Any], errors: list[str]) -> list[EvidenceUnknown]:
    unknowns: list[EvidenceUnknown] = []
    for index, raw in enumerate(raw_unknowns):
        if not isinstance(raw, dict):
            errors.append(f"unknowns[{index}]: must be an object")
            continue
        category = _enum(raw.get("category"), EvidenceCategory, "category", errors, index)
        question = raw.get("question")
        if category is None:
            continue
        if not isinstance(question, str) or not question.strip():
            errors.append(f"unknowns[{index}].question: a non-empty question is required")
            continue
        reason = raw.get("reason")
        unknowns.append(
            EvidenceUnknown(
                category=category,
                question=" ".join(question.split()),
                reason=" ".join(reason.split())
                if isinstance(reason, str) and reason.strip()
                else None,
            )
        )
    return unknowns


def _validate_conflicts(
    raw_conflicts: list[Any],
    *,
    supplied: dict[str, SuppliedSource],
    normalized: dict[str, str],
    errors: list[str],
) -> list[EvidenceConflict]:
    conflicts: list[EvidenceConflict] = []
    for index, raw in enumerate(raw_conflicts):
        label = f"conflicts[{index}]"
        if not isinstance(raw, dict):
            errors.append(f"{label}: must be an object")
            continue
        category = _enum(raw.get("category"), EvidenceCategory, "category", errors, index)
        summary = raw.get("summary")
        raw_ids = raw.get("source_ids")
        if category is None:
            continue
        if not isinstance(summary, str) or not summary.strip():
            errors.append(f"{label}.summary: a non-empty summary is required")
            continue
        if not isinstance(raw_ids, list) or len(raw_ids) < 2:
            errors.append(f"{label}.source_ids: a conflict needs at least two sources")
            continue
        unknown = [sid for sid in raw_ids if not isinstance(sid, str) or sid not in supplied]
        if unknown:
            errors.append(f"{label}.source_ids: {unknown} were not supplied for this candidate")
            continue

        excerpts = (
            _validate_excerpts(
                raw["excerpts"],
                supplied=supplied,
                normalized=normalized,
                errors=errors,
                label=label,
            )
            if isinstance(raw.get("excerpts"), list) and raw["excerpts"]
            else []
        )
        conflicts.append(
            EvidenceConflict(
                category=category,
                summary=" ".join(summary.split()),
                source_ids=[str(sid) for sid in raw_ids],
                excerpts=excerpts,
            )
        )
    return conflicts


def _confidence_inputs(
    claims: list[EvidenceClaim],
    unknowns: list[EvidenceUnknown],
    conflicts: list[EvidenceConflict],
    supplied: dict[str, SuppliedSource],
) -> ConfidenceInputs:
    by_category: dict[str, int] = {}
    by_verification: dict[str, int] = {}
    for claim in claims:
        by_category[claim.category.value] = by_category.get(claim.category.value, 0) + 1
        key = claim.verification_status.value
        by_verification[key] = by_verification.get(key, 0) + 1
    cited = {sid for claim in claims for sid in claim.source_ids}
    return ConfidenceInputs(
        claims_total=len(claims),
        claims_by_category=dict(sorted(by_category.items())),
        claims_by_verification=dict(sorted(by_verification.items())),
        inferred_claims=sum(
            1 for claim in claims if claim.inference_status is InferenceStatus.INFERRED
        ),
        unknowns=len(unknowns),
        conflicts=len(conflicts),
        distinct_domains=len({supplied[sid].reference.domain for sid in cited if sid in supplied}),
    )


def validate_evidence(
    payload: dict[str, Any],
    *,
    company_id: str,
    sources: list[SuppliedSource],
    prompt_version: str,
    provider: str,
    model: str,
    generated_at: datetime,
    website_available: bool,
    extra_warnings: list[str] | None = None,
) -> ValidationOutcome:
    """Turn a model payload into a dossier, or raise with everything that was wrong.

    All issues are collected before raising, so the single retry can send back the whole
    list rather than one problem at a time.
    """
    supplied = {source.source_id: source for source in sources}
    normalized = {sid: normalize_for_match(src.text) for sid, src in supplied.items()}
    errors: list[str] = []

    claims = _validate_claims(
        _as_list(payload, "claims"),
        company_id=company_id,
        supplied=supplied,
        normalized=normalized,
        errors=errors,
    )
    unknowns = _validate_unknowns(_as_list(payload, "unknowns"), errors)
    conflicts = _validate_conflicts(
        _as_list(payload, "conflicts"), supplied=supplied, normalized=normalized, errors=errors
    )

    if errors:
        raise EvidenceValidationError(_error_category(errors), errors)

    warnings = list(extra_warnings or [])
    if not website_available:
        # Attached here rather than by the caller, so the distinction cannot be lost by a
        # stage that forgets to add it.
        warnings.append(
            "No website page could be read for this company. Evidence is missing, which is "
            "not the same as evidence of weakness."
        )
    warnings.extend(
        " ".join(str(item).split())
        for item in _as_list(payload, "warnings")
        if isinstance(item, str) and item.strip()
    )

    cited = {sid for claim in claims for sid in claim.source_ids}
    dossier = EvidenceDossier(
        company_id=company_id,
        claims=claims,
        unknowns=unknowns,
        conflicts=conflicts,
        sources=[source.reference for source in sources],
        source_coverage=SourceCoverage(
            sources_supplied=len(sources),
            sources_cited=len(cited),
            pages_supplied=sum(1 for source in sources if source.role is not None),
            website_available=website_available,
            hn_sources=sum(1 for source in sources if source.role is None),
            truncated_pages=sorted(s.source_id for s in sources if s.truncated),
            supplied_chars=sum(len(source.text) for source in sources),
        ),
        confidence_inputs=_confidence_inputs(claims, unknowns, conflicts, supplied),
        prompt_version=prompt_version,
        provider=provider,
        model=model,
        generated_at=generated_at,
        warnings=warnings,
    )
    return ValidationOutcome(dossier=dossier, warnings=warnings)


def _error_category(errors: list[str]) -> LlmErrorCategory:
    """The most specific category that describes this batch of errors."""
    joined = " ".join(errors)
    if "was not supplied for this candidate" in joined:
        return LlmErrorCategory.UNKNOWN_SOURCE_REFERENCE
    if "does not appear in the text of" in joined:
        return LlmErrorCategory.EXCERPT_NOT_FOUND
    return LlmErrorCategory.SCHEMA_VALIDATION_FAILED
