"""The Anthropic Messages API provider.

Every test runs against ``httpx.MockTransport`` with a fake credential injected into the
environment. ``tests/conftest.py`` blocks sockets and strips real credentials, so a
regression that reintroduced a live call fails loudly instead of spending money.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from vc_scout.llm.anthropic import ANTHROPIC_VERSION, AnthropicProvider
from vc_scout.llm.provider import LlmError, LlmRequest, ModelConfig
from vc_scout.llm.schema import EVIDENCE_SCHEMA, EVIDENCE_TOOL_NAME
from vc_scout.models.enums import LlmErrorCategory

FAKE_KEY = "sk-ant-test-key-not-real-000000"
CONFIG = ModelConfig(model="claude-opus-5", max_tokens=4096, effort="medium")


@pytest.fixture
def api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)


def request(attempt: int = 1) -> LlmRequest:
    return LlmRequest(
        system="System instructions.",
        user_payload="Bounded source material.",
        schema=EVIDENCE_SCHEMA,
        schema_name=EVIDENCE_TOOL_NAME,
        schema_description="Record the evidence you found.",
        config=CONFIG,
        attempt=attempt,
    )


def tool_response(payload: dict[str, Any] | None = None, **extra: Any) -> httpx.Response:
    body = {
        "id": "msg_01ABC",
        "model": "claude-opus-5",
        "stop_reason": "tool_use",
        "usage": {"input_tokens": 1234, "output_tokens": 567},
        "content": [
            {
                "type": "tool_use",
                "id": "toolu_01",
                "name": EVIDENCE_TOOL_NAME,
                "input": payload if payload is not None else {"claims": [], "unknowns": []},
            }
        ],
    }
    body.update(extra)
    return httpx.Response(200, json=body, headers={"request-id": "req_01XYZ"})


def provider(handler: Any, *, record: list[httpx.Request] | None = None) -> AnthropicProvider:
    def wrapped(req: httpx.Request) -> httpx.Response:
        if record is not None:
            record.append(req)
        return handler(req) if callable(handler) else handler

    return AnthropicProvider(
        httpx.Client(transport=httpx.MockTransport(wrapped)),
        base_url="https://api.anthropic.test/v1",
    )


# -- request shape -----------------------------------------------------------


def test_the_request_forces_the_schema_constrained_tool(api_key: None) -> None:
    seen: list[httpx.Request] = []
    provider(tool_response(), record=seen).complete_json(request())
    import json

    body = json.loads(seen[0].content)

    assert seen[0].url.path.endswith("/messages")
    assert body["model"] == "claude-opus-5"
    assert body["system"] == "System instructions."
    assert body["messages"] == [{"role": "user", "content": "Bounded source material."}]
    assert body["tools"][0]["name"] == EVIDENCE_TOOL_NAME
    assert body["tools"][0]["strict"] is True
    assert body["tool_choice"] == {
        "type": "tool",
        "name": EVIDENCE_TOOL_NAME,
        "disable_parallel_tool_use": True,
    }
    assert body["output_config"] == {"effort": "medium"}


def test_no_sampling_parameters_are_sent(api_key: None) -> None:
    """Current Claude models reject temperature outright; determinism comes from effort."""
    seen: list[httpx.Request] = []
    provider(tool_response(), record=seen).complete_json(request())
    import json

    body = json.loads(seen[0].content)
    for banned in ("temperature", "top_p", "top_k"):
        assert banned not in body


def test_required_headers_are_sent(api_key: None) -> None:
    seen: list[httpx.Request] = []
    provider(tool_response(), record=seen).complete_json(request())
    headers = seen[0].headers

    assert headers["anthropic-version"] == ANTHROPIC_VERSION
    assert headers["content-type"] == "application/json"
    assert headers["x-api-key"] == FAKE_KEY


def test_the_persisted_body_carries_no_credential(api_key: None) -> None:
    """build_body is what gets written to the request artifact."""
    body = AnthropicProvider().build_body(request())
    import json

    blob = json.dumps(body)
    assert FAKE_KEY not in blob
    assert "x-api-key" not in blob
    assert "authorization" not in blob.lower()


def test_effort_is_omitted_when_unset(api_key: None) -> None:
    import json

    seen: list[httpx.Request] = []
    plain = LlmRequest(
        system="s",
        user_payload="u",
        schema=EVIDENCE_SCHEMA,
        schema_name=EVIDENCE_TOOL_NAME,
        schema_description="d",
        config=ModelConfig(model="m", effort=None),
    )
    provider(tool_response(), record=seen).complete_json(plain)
    assert "output_config" not in json.loads(seen[0].content)


# -- response parsing --------------------------------------------------------


def test_the_tool_input_usage_and_request_id_are_returned(api_key: None) -> None:
    result = provider(tool_response({"claims": [], "unknowns": [], "conflicts": []})).complete_json(
        request(attempt=2)
    )

    assert result.content == {"claims": [], "unknowns": [], "conflicts": []}
    assert result.provider == "anthropic"
    assert result.model == "claude-opus-5"
    assert result.input_tokens == 1234
    assert result.output_tokens == 567
    assert result.request_id == "req_01XYZ"
    assert result.stop_reason == "tool_use"
    assert result.attempt == 2
    assert result.latency_seconds >= 0.0


def test_the_message_id_is_used_when_no_request_id_header_is_present(api_key: None) -> None:
    response = tool_response()
    response.headers.pop("request-id")
    assert provider(response).complete_json(request()).request_id == "msg_01ABC"


def test_a_response_without_the_tool_block_is_malformed(api_key: None) -> None:
    body = httpx.Response(
        200,
        json={
            "id": "msg_1",
            "model": "m",
            "stop_reason": "refusal",
            "content": [{"type": "text", "text": "I cannot help with that."}],
        },
    )
    with pytest.raises(LlmError) as exc:
        provider(body).complete_json(request())
    assert exc.value.category is LlmErrorCategory.MALFORMED_RESPONSE
    assert "refusal" in exc.value.detail


def test_a_non_json_response_is_malformed(api_key: None) -> None:
    with pytest.raises(LlmError) as exc:
        provider(httpx.Response(200, text="<html>gateway</html>")).complete_json(request())
    assert exc.value.category is LlmErrorCategory.MALFORMED_RESPONSE


# -- errors ------------------------------------------------------------------


def test_a_missing_api_key_is_reported_without_reading_anything(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    seen: list[httpx.Request] = []
    with pytest.raises(LlmError) as exc:
        provider(tool_response(), record=seen).complete_json(request())

    assert exc.value.category is LlmErrorCategory.MISSING_API_KEY
    assert exc.value.retryable is False
    assert seen == [], "no request may be issued without a credential"


def test_api_key_presence_never_returns_the_key(monkeypatch: pytest.MonkeyPatch) -> None:
    instance = AnthropicProvider()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert instance.api_key_present is False
    monkeypatch.setenv("ANTHROPIC_API_KEY", FAKE_KEY)
    assert instance.api_key_present is True
    assert not hasattr(instance, "api_key")


def test_rate_limiting_is_its_own_retryable_category(api_key: None) -> None:
    with pytest.raises(LlmError) as exc:
        provider(httpx.Response(429, json={"error": {"type": "rate_limit_error"}})).complete_json(
            request()
        )
    assert exc.value.category is LlmErrorCategory.PROVIDER_RATE_LIMITED
    assert exc.value.retryable is True


@pytest.mark.parametrize(
    ("status", "retryable"), [(400, False), (401, False), (404, False), (500, True), (529, True)]
)
def test_http_errors_are_categorised_by_retryability(
    api_key: None, status: int, retryable: bool
) -> None:
    body = httpx.Response(status, json={"error": {"type": "invalid_request_error"}})
    with pytest.raises(LlmError) as exc:
        provider(body).complete_json(request())
    assert exc.value.category is LlmErrorCategory.PROVIDER_HTTP_ERROR
    assert exc.value.retryable is retryable
    assert exc.value.status == status


def test_an_error_detail_carries_the_providers_own_message(api_key: None) -> None:
    """The message is what names the offending field on a rejected request.

    An earlier version recorded only the error type, which left a deterministic HTTP 400
    undiagnosable from the persisted artifacts.
    """
    body = httpx.Response(
        400,
        json={
            "error": {
                "type": "invalid_request_error",
                "message": "tools.0.custom.input_schema: enum values must not contain null",
            }
        },
    )
    with pytest.raises(LlmError) as exc:
        provider(body).complete_json(request())
    assert "invalid_request_error" in exc.value.detail
    assert "input_schema" in exc.value.detail
    assert "must not contain null" in exc.value.detail


def test_an_error_message_is_bounded_and_whitespace_collapsed(api_key: None) -> None:
    body = httpx.Response(
        400, json={"error": {"type": "invalid_request_error", "message": "x  \n y " * 500}}
    )
    with pytest.raises(LlmError) as exc:
        provider(body).complete_json(request())
    assert len(exc.value.detail) < 600
    assert exc.value.detail.endswith("...")
    assert "\n" not in exc.value.detail


def test_an_error_message_is_scrubbed_of_key_shaped_text(api_key: None) -> None:
    """Defensive only - the API does not echo credentials."""
    body = httpx.Response(
        400,
        json={"error": {"type": "authentication_error", "message": f"bad key {FAKE_KEY}"}},
    )
    with pytest.raises(LlmError) as exc:
        provider(body).complete_json(request())
    assert FAKE_KEY not in exc.value.detail
    assert "[redacted]" in exc.value.detail


def test_a_missing_error_message_leaves_just_the_type(api_key: None) -> None:
    body = httpx.Response(400, json={"error": {"type": "invalid_request_error"}})
    with pytest.raises(LlmError) as exc:
        provider(body).complete_json(request())
    assert exc.value.detail == "provider returned HTTP 400 (invalid_request_error)"


# -- run-level versus candidate-level failures -------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_deterministic_request_failures_are_run_level(api_key: None, status: int) -> None:
    """A rejected schema, a bad credential or an unknown model fails every request alike."""
    body = httpx.Response(status, json={"error": {"type": "invalid_request_error"}})
    with pytest.raises(LlmError) as exc:
        provider(body).complete_json(request())
    assert exc.value.run_level is True
    assert exc.value.retryable is False


@pytest.mark.parametrize("status", [429, 500, 503, 529])
def test_transient_failures_are_retryable_and_not_run_level(api_key: None, status: int) -> None:
    body = httpx.Response(status, json={"error": {"type": "overloaded_error"}})
    with pytest.raises(LlmError) as exc:
        provider(body).complete_json(request())
    assert exc.value.run_level is False
    assert exc.value.retryable is True


def test_a_request_too_large_is_candidate_specific_not_run_level(api_key: None) -> None:
    """One oversized dossier says nothing about the next candidate."""
    body = httpx.Response(413, json={"error": {"type": "request_too_large"}})
    with pytest.raises(LlmError) as exc:
        provider(body).complete_json(request())
    assert exc.value.run_level is False


def test_a_missing_credential_is_run_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(LlmError) as exc:
        provider(tool_response()).complete_json(request())
    assert exc.value.run_level is True


def test_timeouts_and_connection_faults_are_not_run_level(api_key: None) -> None:
    def timeout(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=req)

    with pytest.raises(LlmError) as exc:
        provider(timeout).complete_json(request())
    assert exc.value.run_level is False
    assert exc.value.retryable is True


# -- the schema this tool actually sends -------------------------------------


def test_the_analysis_schema_carries_no_enum_containing_null() -> None:
    """The exact construct the first live Stage 5 run was rejected for."""
    import json as _json

    from vc_scout.llm.analysis_schema import ANALYSIS_SCHEMA

    def enums(node):
        found = []
        if isinstance(node, dict):
            for key, value in node.items():
                if key == "enum":
                    found.append(value)
                found += enums(value)
        elif isinstance(node, list):
            for item in node:
                found += enums(item)
        return found

    assert all(None not in values for values in enums(ANALYSIS_SCHEMA))
    # ["string", "null"] is fine - the evidence tool used it in a completed live run.
    # It was the enum containing null that was novel.
    assert '"enum"' in _json.dumps(ANALYSIS_SCHEMA), "the vocabularies are still constrained"


def test_the_analysis_schema_uses_only_constructs_the_evidence_schema_proved() -> None:
    """The evidence tool completed a live run; keep the analysis tool inside that surface."""
    from vc_scout.llm.analysis_schema import ANALYSIS_SCHEMA
    from vc_scout.llm.schema import EVIDENCE_SCHEMA

    keywords = frozenset(
        {
            "minimum",
            "maximum",
            "minLength",
            "maxLength",
            "minItems",
            "maxItems",
            "pattern",
            "format",
            "$ref",
            "$defs",
            "allOf",
            "anyOf",
            "oneOf",
            "not",
        }
    )

    def constructs(node, out=None, *, in_properties=False):
        """Constructs used, ignoring keys that are property *names* rather than keywords.

        The analysis schema has a property literally called "maximum"; without this the
        walker would report a numeric constraint that is not there.
        """
        out = out if out is not None else set()
        if isinstance(node, dict):
            for key, value in node.items():
                if not in_properties:
                    if key == "type" and isinstance(value, list):
                        out.add(f"union{sorted(value)}")
                    elif key == "enum":
                        out.add("enum_with_null" if any(v is None for v in value) else "enum")
                    elif key in keywords:
                        out.add(f"keyword:{key}")
                constructs(value, out, in_properties=(key == "properties"))
        elif isinstance(node, list):
            for item in node:
                constructs(item, out)
        return out

    novel = constructs(ANALYSIS_SCHEMA) - constructs(EVIDENCE_SCHEMA)
    assert novel == set(), f"analysis schema introduces unproven constructs: {novel}"


def test_every_object_in_the_analysis_schema_forbids_extra_properties() -> None:
    from vc_scout.llm.analysis_schema import ANALYSIS_SCHEMA

    def objects(node, path="$"):
        found = []
        if isinstance(node, dict):
            if node.get("type") == "object":
                found.append((path, node.get("additionalProperties")))
            for key, value in node.items():
                found += objects(value, f"{path}.{key}")
        elif isinstance(node, list):
            for index, item in enumerate(node):
                found += objects(item, f"{path}[{index}]")
        return found

    for path, value in objects(ANALYSIS_SCHEMA):
        assert value is False, f"{path} does not set additionalProperties: false"


def test_a_timeout_is_its_own_retryable_category(api_key: None) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("too slow", request=req)

    with pytest.raises(LlmError) as exc:
        provider(handler).complete_json(request())
    assert exc.value.category is LlmErrorCategory.PROVIDER_TIMEOUT
    assert exc.value.retryable is True


def test_a_connection_failure_is_a_retryable_http_error(api_key: None) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=req)

    with pytest.raises(LlmError) as exc:
        provider(handler).complete_json(request())
    assert exc.value.category is LlmErrorCategory.PROVIDER_HTTP_ERROR
    assert exc.value.retryable is True
