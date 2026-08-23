"""The provider-neutral interface.

Deliberately small. A provider accepts system instructions, a structured user payload, a
required output schema and a model configuration; it returns parsed structured content
plus the metadata needed to audit the call. Nothing about evidence, scoring or the thesis
appears here - swapping providers must not be able to change what the pipeline believes.

No credential is ever accepted as an argument, stored on an object, returned, logged or
persisted. Providers read them from the environment and nowhere else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from vc_scout.models.enums import LlmErrorCategory

__all__ = [
    "LlmError",
    "LlmProvider",
    "LlmRequest",
    "LlmResult",
    "ModelConfig",
]


class LlmError(Exception):
    """A provider call failed. Carries a category for the evidence report."""

    def __init__(
        self,
        category: LlmErrorCategory,
        detail: str,
        *,
        status: int | None = None,
        retryable: bool = False,
    ) -> None:
        super().__init__(detail)
        self.category = category
        self.detail = detail
        self.status = status
        self.retryable = retryable


@dataclass(frozen=True, slots=True)
class ModelConfig:
    """How the model should be run.

    ``temperature`` is deliberately absent. Sampling parameters are rejected outright by
    the current Claude models, so determinism is expressed through ``effort`` and through
    a fixed prompt and a fixed input ordering instead. See docs/DECISIONS.md D24.
    """

    model: str
    max_tokens: int = 8000
    effort: str | None = "medium"
    timeout_seconds: float = 120.0


@dataclass(frozen=True, slots=True)
class LlmRequest:
    """One schema-constrained call.

    ``user_payload`` is the bounded, structured input for this call. It is passed as data
    and is never merged into ``system``: keeping operator instructions and untrusted source
    content in separate channels is the first half of the prompt-injection defence.
    """

    system: str
    user_payload: str
    schema: dict[str, Any]
    schema_name: str
    schema_description: str
    config: ModelConfig
    attempt: int = 1


@dataclass(frozen=True, slots=True)
class LlmResult:
    """A parsed provider response, plus everything needed to audit the call."""

    content: dict[str, Any]
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    request_id: str | None = None
    stop_reason: str | None = None
    latency_seconds: float = 0.0
    attempt: int = 1
    raw: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class LlmProvider(Protocol):
    """What the pipeline requires of any provider."""

    name: str

    def complete_json(self, request: LlmRequest) -> LlmResult:
        """Return structured content matching ``request.schema``.

        Raises :class:`LlmError` on any failure, categorised for the report.
        """
        ...
