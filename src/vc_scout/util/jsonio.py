"""Atomic, diff-friendly JSON persistence.

Every artifact this pipeline writes must be re-readable after a crash and must produce a
minimal diff between runs, so writes go through a temporary file in the destination
directory followed by an atomic replace, and are serialised with sorted keys.
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

__all__ = ["dumps", "read_json", "write_json"]


def dumps(payload: Any) -> str:
    """Serialise ``payload`` deterministically, with a trailing newline."""
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def write_json(path: Path, payload: Any) -> None:
    """Write ``payload`` to ``path`` atomically, creating parent directories."""
    path.parent.mkdir(parents=True, exist_ok=True)
    text = dumps(payload)
    handle, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        tmp_path.replace(path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def read_json(path: Path) -> Any:
    """Read and parse JSON from ``path``."""
    return json.loads(path.read_text(encoding="utf-8"))
