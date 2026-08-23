"""Versioned runtime prompts.

Prompts are files, not string literals, so that the exact text used for a run can be
hashed, recorded in an artifact and diffed between runs. A prompt change is a behaviour
change and has to be as visible as a code change.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path

__all__ = ["EVIDENCE_PROMPT_VERSION", "prompt_sha256", "prompt_text"]

_PROMPT_DIR = Path(__file__).resolve().parent

EVIDENCE_PROMPT_VERSION = "evidence_v1"


@lru_cache(maxsize=8)
def prompt_text(version: str) -> str:
    """The runtime text of a versioned prompt."""
    path = _PROMPT_DIR / f"{version}.md"
    if not path.is_file():
        raise FileNotFoundError(f"unknown prompt version {version!r}")
    return path.read_text(encoding="utf-8").strip()


@lru_cache(maxsize=8)
def prompt_sha256(version: str) -> str:
    """Content hash of a prompt, recorded alongside every run that used it."""
    return hashlib.sha256(prompt_text(version).encode("utf-8")).hexdigest()
