"""The Anthropic Messages API provider.

A direct httpx client against ``POST /v1/messages``, deliberately not the Anthropic SDK:
this pipeline needs exactly one schema-constrained call shape, and owning the wire format
keeps the request that gets persisted for replay identical to the request that was sent.

Structured output is obtained with **forced tool use**: a single tool carrying the required
JSON schema, with ``tool_choice`` pinned to it and parallel tool use disabled, so the model
has exactly one legal move - emit one ``tool_use`` block whose ``input`` is the schema.
``strict`` is requested so the API validates the payload against the schema before it is
returned.

The API key is read from the environment at call time. It is never stored on the object,
never returned, never logged and never written to an artifact.
"""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from vc_scout.llm.provider import LlmError, LlmRequest, LlmResult
from vc_scout.models.enums import LlmErrorCategory

__all__ = ["API_KEY_ENV", "ANTHROPIC_VERSION", "DEFAULT_BASE_URL", "AnthropicProvider"]

DEFAULT_BASE_URL = "https://api.anthropic.com/v1"
ANTHROPIC_VERSION = "2023-06-01"
API_KEY_ENV = "ANTHROPIC_API_KEY"

#: Retryable per the API's own error taxonomy. Everything else is a request defect and
#: retrying it only spends money.
_RETRYABLE_STATUS = frozenset({408, 409, 429, 500, 502, 503, 529})


class AnthropicProvider:
    """Calls the Messages API with a forced, schema-constrained tool."""

    name = "anthropic"

    def __init__(
        self,
        client: httpx.Client | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        api_key_env: str = API_KEY_ENV,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key_env = api_key_env
        self._owns_client = client is None
        self._client = client or httpx.Client(follow_redirects=False)

    # -- credentials -------------------------------------------------------

    def _api_key(self) -> str:
        """Read the credential from the environment, or fail with a clear category.

        The value is returned to the caller for one request and never retained.
        """
        key = os.environ.get(self.api_key_env, "").strip()
        if not key:
            raise LlmError(
                LlmErrorCategory.MISSING_API_KEY,
                f"{self.api_key_env} is not set in the environment",
            )
        return key

    @property
    def api_key_present(self) -> bool:
        """Whether a credential is available. Never returns the credential."""
        return bool(os.environ.get(self.api_key_env, "").strip())

    # -- request construction ---------------------------------------------

    def build_body(self, request: LlmRequest) -> dict[str, Any]:
        """The request body, without credentials.

        Exposed separately because this is exactly what gets persisted for replay: the
        body carries no headers and therefore no key, so the stored artifact and the sent
        request differ only by the header block.
        """
        body: dict[str, Any] = {
            "model": request.config.model,
            "max_tokens": request.config.max_tokens,
            "system": request.system,
            "messages": [{"role": "user", "content": request.user_payload}],
            "tools": [
                {
                    "name": request.schema_name,
                    "description": request.schema_description,
                    "input_schema": request.schema,
                    # Ask the API to validate the payload against the schema for us.
                    "strict": True,
                }
            ],
            # One legal move: emit this tool, once.
            "tool_choice": {
                "type": "tool",
                "name": request.schema_name,
                "disable_parallel_tool_use": True,
            },
        }
        if request.config.effort:
            # Effort, not temperature. Sampling parameters are rejected by current models.
            body["output_config"] = {"effort": request.config.effort}
        return body

    # -- the call ----------------------------------------------------------

    def complete_json(self, request: LlmRequest) -> LlmResult:
        api_key = self._api_key()
        body = self.build_body(request)
        headers = {
            "content-type": "application/json",
            "anthropic-version": ANTHROPIC_VERSION,
            "x-api-key": api_key,
        }

        started = time.monotonic()
        try:
            response = self._client.post(
                f"{self.base_url}/messages",
                json=body,
                headers=headers,
                timeout=request.config.timeout_seconds,
            )
        except httpx.TimeoutException as exc:
            raise LlmError(
                LlmErrorCategory.PROVIDER_TIMEOUT,
                f"the provider did not respond within {request.config.timeout_seconds}s",
                retryable=True,
            ) from exc
        except httpx.HTTPError as exc:
            raise LlmError(
                LlmErrorCategory.PROVIDER_HTTP_ERROR,
                f"{type(exc).__name__} calling the provider",
                retryable=True,
            ) from exc
        latency = time.monotonic() - started

        # The request id is a response header, not part of the body.
        request_id = response.headers.get("request-id") or response.headers.get("x-request-id")

        if response.status_code == 429:
            raise LlmError(
                LlmErrorCategory.PROVIDER_RATE_LIMITED,
                "the provider rate limited this request",
                status=429,
                retryable=True,
            )
        if response.status_code >= 400:
            raise LlmError(
                LlmErrorCategory.PROVIDER_HTTP_ERROR,
                # The status and error type only. A response body can echo request content.
                f"provider returned HTTP {response.status_code} ({_error_type(response)})",
                status=response.status_code,
                retryable=response.status_code in _RETRYABLE_STATUS,
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise LlmError(
                LlmErrorCategory.MALFORMED_RESPONSE, "the provider response was not JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise LlmError(
                LlmErrorCategory.MALFORMED_RESPONSE, "the provider response was not an object"
            )

        content = _extract_tool_input(payload, request.schema_name)
        usage = payload.get("usage") or {}
        return LlmResult(
            content=content,
            provider=self.name,
            model=str(payload.get("model") or request.config.model),
            input_tokens=_as_int(usage.get("input_tokens")),
            output_tokens=_as_int(usage.get("output_tokens")),
            request_id=request_id or (str(payload["id"]) if payload.get("id") else None),
            stop_reason=payload.get("stop_reason"),
            latency_seconds=round(latency, 4),
            attempt=request.attempt,
            raw=payload,
        )

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> AnthropicProvider:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _error_type(response: httpx.Response) -> str:
    """The API's own error type string, if the body carries one."""
    try:
        payload = response.json()
    except ValueError:
        return "unparseable body"
    if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
        return str(payload["error"].get("type") or "unknown")
    return "unknown"


def _as_int(value: object) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def _extract_tool_input(payload: dict[str, Any], tool_name: str) -> dict[str, Any]:
    """Pull the forced tool's ``input`` out of the response content blocks.

    A refusal or a truncated response leaves no tool_use block; both are reported as a
    malformed response with the stop reason attached, rather than a confusing schema error.
    """
    blocks = payload.get("content")
    if not isinstance(blocks, list):
        raise LlmError(LlmErrorCategory.MALFORMED_RESPONSE, "response carried no content blocks")

    for block in blocks:
        if (
            isinstance(block, dict)
            and block.get("type") == "tool_use"
            and block.get("name") == tool_name
            and isinstance(block.get("input"), dict)
        ):
            return dict(block["input"])

    stop_reason = payload.get("stop_reason")
    raise LlmError(
        LlmErrorCategory.MALFORMED_RESPONSE,
        f"response contained no {tool_name!r} tool_use block (stop_reason={stop_reason!r})",
    )
