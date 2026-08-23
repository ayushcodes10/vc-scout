"""VC Scout - an AI-augmented investment triage pipeline for a seed-stage VC firm.

Stage boundaries, artifact contracts and the deterministic recommendation policy are
documented in ``docs/PLAN.md``.
"""

__all__ = ["SCHEMA_VERSION", "__version__"]

__version__ = "0.1.0"

#: Bumped whenever the on-disk artifact contract changes in a non-additive way.
SCHEMA_VERSION = "1.1.0"
