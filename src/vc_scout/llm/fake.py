"""A deterministic provider for tests, replay and offline demonstration.

It makes no network call and reads no credential. Behaviour is supplied by the test: a
scripted queue of responses, a callable, or - by default - a trivially valid empty dossier.
That lets the whole validation, retry and persistence path be exercised without ever
depending on what a real model happens to say.

It also records every request it received, so a test can assert on what the pipeline
actually sent - including that untrusted source text stayed out of the system prompt.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from vc_scout.llm.provider import LlmError, LlmRequest, LlmResult

__all__ = ["FakeProvider", "Scripted"]

#: A scripted turn: either a payload to return, or an error to raise.
Scripted = dict[str, Any] | LlmError


class FakeProvider:
    """Returns scripted structured content, in order, and remembers every request."""

    name = "fake"

    def __init__(
        self,
        responses: Iterable[Scripted] | None = None,
        *,
        handler: Callable[[LlmRequest], Scripted] | None = None,
        model: str = "fake-model-1",
    ) -> None:
        self._queue: list[Scripted] = list(responses or [])
        self._handler = handler
        self.model = model
        #: Every request received, in order. Tests assert against this.
        self.requests: list[LlmRequest] = []

    @property
    def call_count(self) -> int:
        return len(self.requests)

    def complete_json(self, request: LlmRequest) -> LlmResult:
        self.requests.append(request)

        if self._handler is not None:
            outcome = self._handler(request)
        elif self._queue:
            outcome = self._queue.pop(0)
        else:
            outcome = _empty_dossier()

        if isinstance(outcome, LlmError):
            raise outcome

        return LlmResult(
            content=outcome,
            provider=self.name,
            model=self.model,
            input_tokens=len(request.user_payload) // 4,
            output_tokens=64,
            request_id=f"fake-req-{len(self.requests):03d}",
            stop_reason="tool_use",
            latency_seconds=0.0,
            attempt=request.attempt,
            raw={"content": [{"type": "tool_use", "name": request.schema_name, "input": outcome}]},
        )


def _empty_dossier() -> dict[str, Any]:
    """A structurally valid response that asserts nothing about any company."""
    return {"claims": [], "unknowns": [], "conflicts": [], "warnings": []}
