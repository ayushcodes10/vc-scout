"""Deterministic Markdown rendering.

The last stage of the pipeline makes no decisions and calls no model. It reads validated
artifacts and lays them out: the score was computed in Python, the confidence by the
policy, the recommendation by the policy, and the narrative by the analysis model under a
validator. Rendering adds nothing to that record - it makes it readable, and it makes
every reader-facing citation traceable back to a URL.

Modules here are deliberately small and layered:

* :mod:`~vc_scout.render.markdown` - neutralise untrusted text, render safe links.
* :mod:`~vc_scout.render.sources` - map internal source IDs to reader-facing markers.
* :mod:`~vc_scout.render.call` - the deterministic one-sentence call and its wording.
* :mod:`~vc_scout.render.memo` / :mod:`~vc_scout.render.ranking` - view models.
* :mod:`~vc_scout.render.engine` - the Jinja environment and output normalisation.
"""

from __future__ import annotations

__all__ = ["TEMPLATE_VERSION"]

#: Re-exported for convenience. Defined in :mod:`vc_scout.render.engine`.
TEMPLATE_VERSION = "memo_v1"
