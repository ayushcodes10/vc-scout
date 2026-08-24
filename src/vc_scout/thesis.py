"""The firm's investment thesis, as versioned configuration.

The thesis is a file, not a string literal in a prompt, for the same reason the prompts
are: a change to what the firm is looking for is a behaviour change, and it has to be as
visible and as diffable as a code change. Every analysis artifact records the version and
the content hash of the thesis it was produced under.
"""

from __future__ import annotations

import hashlib

__all__ = ["THESIS_TEXT", "THESIS_VERSION", "thesis_sha256"]

THESIS_VERSION = "thesis_v1"

THESIS_TEXT = (
    "We invest in seed-stage, AI-native software companies that automate recurring, "
    "revenue-critical workflows for SMBs. The product should produce measurable value "
    "within 30 days, integrate into an existing system of record and develop "
    "defensibility through proprietary workflow data, distribution, integrations or "
    "operational depth rather than relying only on model access."
)


def thesis_sha256() -> str:
    """Content hash of the active thesis, recorded alongside every analysis."""
    return hashlib.sha256(THESIS_TEXT.encode("utf-8")).hexdigest()
