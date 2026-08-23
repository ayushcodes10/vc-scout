"""Runtime settings.

Secrets are read from the environment and are never accepted as command-line arguments,
never stored on a settings object and never written to an artifact. :meth:`Settings.
api_key_present` reports only whether a key exists.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from vc_scout.store import DEFAULT_RUNS_ROOT

__all__ = [
    "API_KEY_ENV",
    "DEFAULT_EFFORT",
    "DEFAULT_LIMIT",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MODEL",
    "DEFAULT_PROVIDER",
    "DEFAULT_TIMEOUT_SECONDS",
    "MODEL_ENV",
    "Settings",
]

API_KEY_ENV = "ANTHROPIC_API_KEY"
MODEL_ENV = "LLM_MODEL"

DEFAULT_PROVIDER = "anthropic"
#: Sampling parameters are rejected by current Claude models, so run determinism comes
#: from a fixed prompt, a fixed source ordering and a fixed effort level - not from
#: temperature. See docs/DECISIONS.md D24.
DEFAULT_MODEL = "claude-opus-5"
DEFAULT_EFFORT = "medium"
DEFAULT_MAX_TOKENS = 8000
DEFAULT_TIMEOUT_SECONDS = 120.0
DEFAULT_LIMIT = 15

#: Bounds on how many candidates a run may triage, per the assignment.
MIN_CANDIDATES = 10
MAX_CANDIDATES = 20


@dataclass(frozen=True, slots=True)
class Settings:
    """Resolved configuration for one invocation."""

    runs_root: Path = field(default_factory=lambda: DEFAULT_RUNS_ROOT)
    provider: str = DEFAULT_PROVIDER
    model: str = DEFAULT_MODEL
    limit: int = DEFAULT_LIMIT

    @classmethod
    def from_env(
        cls,
        *,
        runs_root: Path | None = None,
        provider: str | None = None,
        model: str | None = None,
        limit: int | None = None,
    ) -> Settings:
        """Build settings from explicit arguments, falling back to environment, then defaults."""
        env_root = os.environ.get("VC_SCOUT_RUNS_ROOT")
        return cls(
            runs_root=runs_root or (Path(env_root) if env_root else DEFAULT_RUNS_ROOT),
            provider=provider or os.environ.get("VC_SCOUT_PROVIDER", DEFAULT_PROVIDER),
            model=model or os.environ.get(MODEL_ENV, DEFAULT_MODEL),
            limit=limit if limit is not None else DEFAULT_LIMIT,
        )

    @property
    def api_key_present(self) -> bool:
        """Whether a provider credential is available. Never returns the key itself."""
        return bool(os.environ.get(API_KEY_ENV, "").strip())
