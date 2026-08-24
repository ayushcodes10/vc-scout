"""The Jinja environment.

Autoescaping is off, and that is not an oversight. Jinja's autoescape produces *HTML*
entities, which in a Markdown document are visible noise at best and broken output at
worst. The escaping a Markdown memo needs is Markdown-aware and happens earlier, in
:mod:`vc_scout.render.markdown`, before any value reaches a template - by the time Jinja
sees a string it is already safe to place. ``StrictUndefined`` backs that up: a template
that reaches for a value the view model does not define fails the render rather than
silently emitting an empty cell.
"""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

__all__ = ["TEMPLATE_DIR", "TEMPLATE_VERSION", "environment", "render_template"]

TEMPLATE_DIR = Path(__file__).resolve().parent.parent / "templates"

#: Bumped whenever the rendered shape changes. Recorded in the recommendation report so a
#: memo can be traced to the template that produced it.
TEMPLATE_VERSION = "memo_v1"

_ENVIRONMENT: Environment | None = None


def environment() -> Environment:
    """The shared, lazily built environment."""
    global _ENVIRONMENT  # noqa: PLW0603 - one process-wide template cache, built once.
    if _ENVIRONMENT is None:
        _ENVIRONMENT = Environment(
            loader=FileSystemLoader(TEMPLATE_DIR),
            autoescape=False,  # noqa: S701 - Markdown escaping happens in render.markdown.
            undefined=StrictUndefined,
            trim_blocks=True,
            lstrip_blocks=True,
            keep_trailing_newline=True,
        )
    return _ENVIRONMENT


def render_template(name: str, **context: object) -> str:
    """Render ``name`` and normalise the result.

    Trailing whitespace is stripped from every line and runs of blank lines are collapsed,
    so template layout changes that do not change content also do not change bytes.
    """
    raw = environment().get_template(name).render(**context)
    lines = [line.rstrip() for line in raw.replace("\r\n", "\n").split("\n")]
    out: list[str] = []
    for line in lines:
        if not line and out and not out[-1]:
            continue
        out.append(line)
    while out and not out[-1]:
        out.pop()
    return "\n".join(out) + "\n"
