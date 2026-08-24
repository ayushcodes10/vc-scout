"""The provider-facing schema's size and shape.

The first controlled live run of Stage 5 was rejected with::

    HTTP 400 invalid_request_error: The compiled grammar is too large, which would cause
    performance issues. Simplify your tool schemas or reduce the number of strict tools.

These tests keep the schema inside the budget that rejection established, and prove that
compacting it did not cost any output field. The semantic contract lives in
``analysis_validation.py`` and is asserted separately.
"""

from __future__ import annotations

import json

import pytest

from tests.unit.analysis_fixtures import analysis_payload, dossier, unknown_ref
from vc_scout.llm.analysis_schema import (
    ANALYSIS_SCHEMA,
    ANALYSIS_SCHEMA_VERSION,
    ANALYSIS_TOOL_NAME,
    SECTION_KINDS,
)
from vc_scout.llm.analysis_validation import validate_analysis
from vc_scout.llm.anthropic import AnthropicProvider
from vc_scout.llm.provider import LlmRequest, ModelConfig
from vc_scout.llm.schema import EVIDENCE_SCHEMA
from vc_scout.models.enums import AssessmentStatus, Recommendation, RubricDimension, ThesisFit
from vc_scout.rubric import RUBRIC

#: The evidence tool compiled successfully in a live run at this size, so it is the only
#: empirical reference point available. The analysis tool must stay at or below it.
EVIDENCE_BYTES = len(json.dumps(EVIDENCE_SCHEMA, separators=(",", ":")))

#: A hard ceiling with headroom below the proven-good size. Raising this is a decision that
#: should be made deliberately and re-verified against the API, not absorbed silently.
MAX_SCHEMA_BYTES = 2_400
#: Distinct object shapes drive grammar rules more than raw bytes do.
MAX_OBJECT_SHAPES = 4


def serialized() -> str:
    return json.dumps(ANALYSIS_SCHEMA, separators=(",", ":"))


def walk(node, path="$"):
    out = []
    if isinstance(node, dict):
        out.append((path, node))
        for key, value in node.items():
            out += walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, item in enumerate(node):
            out += walk(item, f"{path}[{index}]")
    return out


# -- the budget --------------------------------------------------------------


def test_the_serialized_schema_stays_within_budget() -> None:
    size = len(serialized())
    assert size <= MAX_SCHEMA_BYTES, (
        f"the provider schema is {size} bytes, over the {MAX_SCHEMA_BYTES}-byte budget. "
        "The API rejected an earlier version for compiled-grammar size; shrink the schema "
        "and move the rule into the local validator rather than raising this number."
    )


def test_the_schema_is_no_larger_than_the_one_the_api_proved() -> None:
    assert len(serialized()) <= EVIDENCE_BYTES


def test_distinct_object_shapes_stay_few() -> None:
    """Shape count drives grammar rules; six of them is what broke the budget."""
    shapes = [node for _, node in walk(ANALYSIS_SCHEMA) if node.get("type") == "object"]
    assert len(shapes) <= MAX_OBJECT_SHAPES


def test_the_schema_carries_no_descriptions() -> None:
    """Every instruction lives in the versioned prompt, where it costs the grammar nothing."""
    assert '"description"' not in serialized()


# -- structural guarantees ---------------------------------------------------


def test_every_object_forbids_additional_properties() -> None:
    for path, node in walk(ANALYSIS_SCHEMA):
        if node.get("type") == "object":
            assert node.get("additionalProperties") is False, f"{path} permits extra keys"


def test_no_unsupported_construct_is_used() -> None:
    """Strict mode rejects numeric and length constraints, and recursion."""
    blob = serialized()
    for keyword in (
        '"minimum"',
        '"maximum":',
        '"minLength"',
        '"maxLength"',
        '"minItems"',
        '"maxItems"',
        '"pattern"',
        '"$ref"',
        '"not"',
    ):
        assert keyword not in blob, f"{keyword} is not supported in a strict tool schema"


def test_no_enum_contains_null() -> None:
    for _, node in walk(ANALYSIS_SCHEMA):
        if "enum" in node:
            assert None not in node["enum"]


def test_exactly_one_forced_strict_tool_is_sent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test-not-real-0000")
    body = AnthropicProvider().build_body(
        LlmRequest(
            system="s",
            user_payload="u",
            schema=ANALYSIS_SCHEMA,
            schema_name=ANALYSIS_TOOL_NAME,
            schema_description="d",
            config=ModelConfig(model="claude-sonnet-5"),
        )
    )
    assert len(body["tools"]) == 1
    assert body["tools"][0]["strict"] is True
    assert body["tool_choice"] == {
        "type": "tool",
        "name": ANALYSIS_TOOL_NAME,
        "disable_parallel_tool_use": True,
    }


# -- nothing useful was deleted ----------------------------------------------


def test_every_vocabulary_is_still_constrained_by_the_schema() -> None:
    props = ANALYSIS_SCHEMA["properties"]
    component = props["score_components"]["items"]["properties"]
    assert props["thesis_fit"]["enum"] == [m.value for m in ThesisFit]
    assert props["model_suggested_recommendation"]["enum"] == [m.value for m in Recommendation]
    assert component["component"]["enum"] == [m.value for m in RubricDimension]
    assert component["assessment_status"]["enum"] == [m.value for m in AssessmentStatus]
    assert set(props["sections"]["items"]["properties"]["kind"]["enum"]) == set(SECTION_KINDS)


def test_the_schema_still_accepts_every_output_field_the_analysis_persists() -> None:
    """Compacting moved fields; it did not drop them."""
    props = set(ANALYSIS_SCHEMA["properties"])
    assert props == {
        "plain_language_product",
        "buyer",
        "workflow",
        "thesis_fit",
        "sections",
        "score_components",
        "open_questions",
        "recommendation_changers",
        "model_suggested_recommendation",
        "identity_warnings",
        "analysis_warnings",
    }
    # The six shapes that were merged are all still expressible, via `kind`.
    assert set(SECTION_KINDS) == {
        "team",
        "product",
        "market",
        "thesis",
        "risk",
        "competitor",
        "corroborated",
    }


def test_a_payload_in_the_compact_shape_still_passes_the_production_validator() -> None:
    subject = dossier(claims=4, unknowns=2)
    claim, unknown = subject.claims[0].claim_id, unknown_ref(subject)
    payload = analysis_payload(
        subject,
        extra_sections=[
            {
                "kind": "risk",
                "text": "A recorded gap.",
                "evidence_claim_ids": [],
                "unknown_references": [unknown],
            },
            {
                "kind": "competitor",
                "text": "A named rival.",
                "evidence_claim_ids": [claim],
                "unknown_references": [],
            },
            {
                "kind": "corroborated",
                "text": "A corroborated fact.",
                "evidence_claim_ids": [claim],
                "unknown_references": [],
            },
        ],
    )
    result = validate_analysis(payload, dossier=subject)

    # Every output the persisted analysis needs survives the round trip.
    assert result.plain_language_product and result.buyer and result.workflow
    assert result.team_assessment and result.product_assessment and result.market_assessment
    assert result.thesis_assessment.verdict is ThesisFit.ALIGNED
    assert len(result.risks) == 1 and result.risks[0].unknown_references == [unknown]
    assert len(result.competitive_observations) == 1
    assert len(result.corroborated_findings) == 1
    assert result.corroborated_findings[0].fact == "A corroborated fact."
    assert len(result.score_components) == len(RUBRIC) == 7
    assert result.open_questions and result.recommendation_changers


def test_the_rubric_maxima_come_from_configuration_not_the_model() -> None:
    """`maximum` is no longer in the schema; the validator supplies it."""
    assert "maximum" not in ANALYSIS_SCHEMA["properties"]["score_components"]["items"]["properties"]
    subject = dossier(claims=4)
    result = validate_analysis(analysis_payload(subject), dossier=subject)
    assert {c.component: c.maximum for c in result.score_components} == {
        spec.key: spec.max_points for spec in RUBRIC
    }


def test_the_schema_version_records_the_compaction() -> None:
    assert ANALYSIS_SCHEMA_VERSION == "analysis-schema-2"
