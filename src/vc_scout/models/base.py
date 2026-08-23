"""Shared Pydantic configuration.

Two rules apply to every model in this package:

* ``extra="forbid"`` - an unexpected key in an artifact or an LLM response is a contract
  violation and must fail loudly rather than be silently dropped.
* ``frozen=True`` for artifact content - once written, a record is evidence. Accumulators
  that are built up during a run (the manifest) are the documented exception.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, model_validator

from vc_scout import SCHEMA_VERSION

__all__ = ["ArtifactModel", "MutableModel", "RecordModel"]


class RecordModel(BaseModel):
    """An immutable record nested inside an artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_enum_values=False)

    @model_validator(mode="before")
    @classmethod
    def _drop_computed_fields(cls, data: Any) -> Any:
        """Ignore computed fields when reading an artifact back.

        Computed fields such as ``char_count`` are serialised for the benefit of anyone
        reading the JSON, but they are derived, not inputs. Without this, ``extra="forbid"``
        would reject the very documents this package writes.
        """
        if isinstance(data, dict) and cls.model_computed_fields:
            return {k: v for k, v in data.items() if k not in cls.model_computed_fields}
        return data


class ArtifactModel(RecordModel):
    """A record that is persisted as a top-level JSON document."""

    schema_version: str = SCHEMA_VERSION


class MutableModel(BaseModel):
    """An accumulator assembled during a run before being persisted."""

    model_config = ConfigDict(extra="forbid", frozen=False, use_enum_values=False)
