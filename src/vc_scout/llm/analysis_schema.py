"""The compact provider-facing schema for the analysis tool.

This schema exists for one purpose: to be **compiled into a decoding grammar** by the API.
It is not the analysis contract. The contract is
:func:`vc_scout.llm.analysis_validation.validate_analysis` together with the models in
:mod:`vc_scout.models.analysis`, and it is unchanged.

The first controlled live run was rejected with::

    HTTP 400 invalid_request_error: The compiled grammar is too large, which would cause
    performance issues. Simplify your tool schemas or reduce the number of strict tools.

So this schema is written to be small in the two ways that drive grammar size - the number
of distinct object shapes, and the size of the text carried alongside them:

* **One shape for every grounded statement.** The three narrative sections, the thesis
  rationale, the risks, the competitive observations and the corroborated findings were
  six distinct object definitions. They are now one ``sections`` array whose items carry a
  ``kind``, which the local validator partitions back out.
* **No descriptions.** Every instruction they carried is in the versioned prompt, where it
  belongs and where it costs the grammar nothing.
* **Nothing the model should not be deciding.** ``maximum`` is a rubric constant and is no
  longer asked for; the validator fills it from the configured rubric.
* **Primitive arrays** wherever the relationship between values can be checked locally.

What is *not* done here: strict mode stays on, there is still exactly one forced tool, no
free-form JSON is accepted, and no candidate is split across calls. Every rule the API can
no longer express - the seven exact components, the rubric maxima, the status ceilings, the
total, reference integrity, grounding, and the two-or-three changers - is enforced by the
local validator, which was always the authority.
"""

from __future__ import annotations

from typing import Any

from vc_scout.models.enums import AssessmentStatus, Recommendation, RubricDimension, ThesisFit

__all__ = [
    "ANALYSIS_SCHEMA",
    "ANALYSIS_SCHEMA_VERSION",
    "ANALYSIS_TOOL_NAME",
    "SECTION_KINDS",
    "SINGULAR_KINDS",
    "SectionKind",
]

#: Bumped from analysis-schema-1 when the provider-facing shape was compacted.
ANALYSIS_SCHEMA_VERSION = "analysis-schema-2"
ANALYSIS_TOOL_NAME = "record_analysis"


class SectionKind:
    """What a grounded section is. One object shape covers all of them."""

    TEAM = "team"
    PRODUCT = "product"
    MARKET = "market"
    THESIS = "thesis"
    RISK = "risk"
    COMPETITOR = "competitor"
    CORROBORATED = "corroborated"


#: The four that must appear exactly once; the rest may repeat or be absent.
SINGULAR_KINDS = (SectionKind.TEAM, SectionKind.PRODUCT, SectionKind.MARKET, SectionKind.THESIS)

SECTION_KINDS = (
    *SINGULAR_KINDS,
    SectionKind.RISK,
    SectionKind.COMPETITOR,
    SectionKind.CORROBORATED,
)

_STRINGS: dict[str, Any] = {"type": "array", "items": {"type": "string"}}

#: One object shape for every grounded statement in the analysis.
_SECTION: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind", "text", "evidence_claim_ids", "unknown_references"],
    "properties": {
        "kind": {"type": "string", "enum": list(SECTION_KINDS)},
        "text": {"type": "string"},
        "evidence_claim_ids": _STRINGS,
        "unknown_references": _STRINGS,
    },
}

#: One object shape for a scored dimension. ``maximum`` is deliberately absent: it is a
#: rubric constant, and asking the model to echo it only grew the grammar.
_COMPONENT: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "component",
        "score",
        "assessment_status",
        "rationale",
        "evidence_claim_ids",
        "unknown_references",
    ],
    "properties": {
        "component": {"type": "string", "enum": [member.value for member in RubricDimension]},
        "score": {"type": "integer"},
        "assessment_status": {
            "type": "string",
            "enum": [member.value for member in AssessmentStatus],
        },
        "rationale": {"type": "string"},
        "evidence_claim_ids": _STRINGS,
        "unknown_references": _STRINGS,
        "caveats": _STRINGS,
    },
}

ANALYSIS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "plain_language_product",
        "thesis_fit",
        "sections",
        "score_components",
        "recommendation_changers",
    ],
    "properties": {
        "plain_language_product": {"type": "string"},
        "buyer": {"type": ["string", "null"]},
        "workflow": {"type": ["string", "null"]},
        "thesis_fit": {"type": "string", "enum": [member.value for member in ThesisFit]},
        "sections": {"type": "array", "items": _SECTION},
        "score_components": {"type": "array", "items": _COMPONENT},
        "open_questions": _STRINGS,
        "recommendation_changers": _STRINGS,
        "model_suggested_recommendation": {
            "type": "string",
            "enum": [member.value for member in Recommendation],
        },
        "identity_warnings": _STRINGS,
        "analysis_warnings": _STRINGS,
    },
}
