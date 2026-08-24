"""A deterministic provider for tests, replay and offline acceptance runs.

It makes no network call and reads no credential. Behaviour is supplied by the caller - a
scripted queue of responses, or a callable - and when neither is given it **derives** a
schema-valid response from the request itself: which tool was asked for, which identifiers
the payload made available, and which rubric the payload carried.

That derivation matters. A fake that returns one fixed shape can only ever satisfy one
call site, and the first offline acceptance run of the analysis stage failed for all
fifteen candidates because the default was shaped for the evidence tool. The default now
answers whichever tool it was actually given.

Two rules the derived responses hold to:

* **Nothing is invented.** Claim IDs and unknown references are read out of the supplied
  payload, so a derived response can only ever cite material the caller really supplied.
  A dossier with no claims yields an analysis that says so, rather than one that makes
  something up.
* **No company-specific knowledge.** Everything comes from the request - the schema's
  vocabularies and the payload's own text.

It also records every request it received, so a test can assert on what the pipeline
actually sent.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from typing import Any

from vc_scout.llm.analysis_schema import ANALYSIS_TOOL_NAME
from vc_scout.llm.provider import LlmError, LlmRequest, LlmResult
from vc_scout.models.analysis import STATUS_CEILING_RATIO
from vc_scout.models.enums import AssessmentStatus
from vc_scout.rubric import RUBRIC

__all__ = ["FakeProvider", "Scripted"]

#: A scripted turn: either a payload to return, or an error to raise.
Scripted = dict[str, Any] | LlmError

_CLAIM_ID = re.compile(r"^claim_id:\s*(ev-[0-9a-f]{12})\s*$", re.MULTILINE)
_UNKNOWN_ID = re.compile(r"^unknown_reference:\s*(unk-[0-9a-f]{12})\b", re.MULTILINE)
#: The rubric as the analysis payload renders it: "- pain_roi: 20 - Pain and measurable ROI."
_RUBRIC_LINE = re.compile(r"^- ([a-z_]+): (\d+) - ", re.MULTILINE)

#: How much of a dimension's maximum a derived analysis awards, as a function of how much
#: evidence the dossier carries. Deliberately conservative and bounded: a fake should not
#: manufacture a strong investment case, and it must never exceed a status ceiling.
_BASE_SHARE = 0.25
_SHARE_PER_CLAIM = 0.0625
_MAX_SHARE = 0.75


class FakeProvider:
    """Returns scripted structured content, or derives one, and remembers every request."""

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
            outcome = derive_response(request)

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


def derive_response(request: LlmRequest) -> dict[str, Any]:
    """A schema-valid response for whichever tool ``request`` asked for."""
    if request.schema_name == ANALYSIS_TOOL_NAME:
        return _analysis_response(request)
    return _evidence_response(request)


#: The supplied-source blocks the evidence stage renders, and the fields inside one.
_SOURCE_BLOCK = re.compile(
    r"----- BEGIN UNTRUSTED SOURCE (?P<sid>src-[0-9a-f]{12}) -----\n(?P<body>.*?)\n"
    r"----- END UNTRUSTED SOURCE (?P=sid) -----",
    re.DOTALL,
)
_BLOCK_FIELD = re.compile(r"^(?P<key>kind|page_role): (?P<value>.+)$", re.MULTILINE)

#: Roles map to the evidence category the page actually speaks to.
_ROLE_CATEGORY = {
    "homepage": "product",
    "product": "product",
    "pricing": "product",
    "customers": "traction",
    "about": "team",
    "team": "team",
}

#: A quotable span: long enough to be a citation, short enough to stay one.
_MIN_EXCERPT, _MAX_EXCERPT, _MAX_CLAIMS = 40, 200, 5


def _quotable(text: str) -> str | None:
    """The first sentence-length span of ``text``, copied verbatim.

    Verbatim is the whole point. The excerpt is verified against the supplied source before
    a dossier is written, so a fake that paraphrased would be rejected by the same check
    that catches a model inventing a quotation.
    """
    for candidate in re.split(r"(?<=[.!?])\s+", " ".join(text.split())):
        if _MIN_EXCERPT <= len(candidate) <= _MAX_EXCERPT:
            return candidate
    return None


def _evidence_response(request: LlmRequest) -> dict[str, Any]:
    """Evidence quoted verbatim out of the sources the request supplied.

    Asserts nothing the payload does not contain: every claim is "the supplied page states
    X", where X is a span copied from that page. That keeps an offline run honest - the
    dossier it produces is a real citation chain over real fixture text - while inventing
    no fact about any company.
    """
    payload = request.user_payload
    claims: list[dict[str, Any]] = []
    seen_categories: set[str] = set()

    for match in _SOURCE_BLOCK.finditer(payload):
        if len(claims) >= _MAX_CLAIMS:
            break
        body = match.group("body")
        fields = {f.group("key"): f.group("value").strip() for f in _BLOCK_FIELD.finditer(body)}
        text = body.split("text:\n", 1)[-1]
        excerpt = _quotable(text)
        if excerpt is None:
            continue
        community = fields.get("kind", "").startswith("hn_")
        category = (
            "traction" if community else _ROLE_CATEGORY.get(fields.get("page_role", ""), "product")
        )
        seen_categories.add(category)
        claims.append(
            {
                "category": category,
                "claim": f"The supplied {fields.get('page_role') or fields.get('kind', 'source')} "
                f"states: {excerpt}",
                "excerpts": [{"source_id": match.group("sid"), "excerpt": excerpt}],
                "verification_status": "community_signal" if community else "company_claim",
                "inference_status": "explicit",
            }
        )

    unknowns = [
        {"category": category, "question": question}
        for category, question in (
            ("team", "Who works at this company, and what did they do before?"),
            ("traction", "How many customers does this company have?"),
            ("market", "How large is the buyer population this product addresses?"),
        )
        if category not in seen_categories
    ]
    return {"claims": claims, "unknowns": unknowns, "conflicts": [], "warnings": []}


def _rubric_from(payload: str) -> dict[str, int]:
    """The rubric as the request rendered it, falling back to the configured one.

    Reading it from the payload keeps the derivation honest - the fake scores exactly the
    dimensions it was asked about. The fallback covers a caller that built a request by
    hand without the rubric block.
    """
    found = {name: int(maximum) for name, maximum in _RUBRIC_LINE.findall(payload)}
    return found or {spec.key.value: spec.max_points for spec in RUBRIC}


def _dimensions_from(request: LlmRequest) -> list[str]:
    """The dimension vocabulary the schema declares, in its declared order."""
    try:
        enum = request.schema["properties"]["score_components"]["items"]["properties"]["component"][
            "enum"
        ]
    except (KeyError, TypeError):
        return [spec.key.value for spec in RUBRIC]
    return [str(value) for value in enum]


def _ceiling(maximum: int, status: AssessmentStatus) -> int:
    return int(maximum * STATUS_CEILING_RATIO[status])


def _analysis_response(request: LlmRequest) -> dict[str, Any]:
    """Derive a valid analysis from the identifiers and rubric the payload supplied.

    Every reference is read out of the request, so nothing is cited that was not offered.
    When the payload carries no claims - a zero-claim dossier - every dimension comes back
    ``not_assessable``, anchored on the recorded unknowns, with a narrative that says the
    evidence was insufficient rather than inventing a case.
    """
    payload = request.user_payload
    claim_ids = _CLAIM_ID.findall(payload)
    unknown_ids = _UNKNOWN_ID.findall(payload)
    rubric = _rubric_from(payload)
    dimensions = _dimensions_from(request)

    has_evidence = bool(claim_ids)
    anchor_claim = claim_ids[0] if has_evidence else None
    anchor_unknown = unknown_ids[0] if unknown_ids else None
    share = min(_MAX_SHARE, _BASE_SHARE + _SHARE_PER_CLAIM * len(claim_ids))
    claims_for = [anchor_claim] if anchor_claim else []
    unknowns_for = [] if anchor_claim else ([anchor_unknown] if anchor_unknown else [])

    components: list[dict[str, Any]] = []
    for index, name in enumerate(dimensions):
        maximum = rubric.get(name, 0)
        # A dimension is only treated as supported while there is a distinct claim to
        # anchor it. Beyond that the evidence has run out, which is not a finding.
        supported = index < len(claim_ids)
        status = AssessmentStatus.SUPPORTED if supported else AssessmentStatus.NOT_ASSESSABLE
        ceiling = _ceiling(maximum, status)
        if supported:
            score = min(ceiling, round(maximum * share))
        elif has_evidence:
            # Some evidence exists, just not for this dimension: a small non-zero score
            # reflects that the company is not wholly unknown.
            score = min(ceiling, maximum // 5)
        else:
            # Nothing at all was established. Zero is the honest number here, and the
            # zero-claim guardrail - not the score - is what produces the recommendation.
            score = 0
        components.append(
            {
                "component": name,
                "score": score,
                "assessment_status": status.value,
                "rationale": (
                    "Derived offline from the supplied dossier. The score reflects what the "
                    "cited evidence establishes and the uncertainty that remains."
                    if supported
                    else "The supplied dossier does not establish this dimension, so it is "
                    "recorded as unassessed rather than scored against the company."
                ),
                "evidence_claim_ids": [claim_ids[index]] if supported else [],
                "unknown_references": (
                    [] if supported else ([anchor_unknown] if anchor_unknown else [])
                ),
                "caveats": [],
            }
        )

    body = (
        "Offline derived assessment based only on the supplied evidence."
        if has_evidence
        else "The supplied dossier established nothing about this company."
    )
    sections = [
        {
            "kind": kind,
            "text": body,
            "evidence_claim_ids": list(claims_for),
            "unknown_references": list(unknowns_for),
        }
        for kind in ("team", "product", "market")
    ]
    sections.append(
        {
            "kind": "thesis",
            "text": (
                "This offline response does not assess thesis fit; the verdict is left "
                "undetermined."
            ),
            "evidence_claim_ids": list(claims_for),
            "unknown_references": list(unknowns_for),
        }
    )
    if anchor_unknown:
        sections.append(
            {
                "kind": "risk",
                "text": "The supplied dossier leaves recorded questions unanswered.",
                "evidence_claim_ids": [],
                "unknown_references": [anchor_unknown],
            }
        )
    # No `competitor` or `corroborated` entries are ever emitted: a competitor may only be
    # named from a claim this response cannot read, and corroboration is a semantic judgment
    # a fake cannot make.

    return {
        "plain_language_product": (
            "An offline derived description based only on the supplied evidence."
            if has_evidence
            else "The supplied evidence does not establish what this company does."
        ),
        "buyer": "Derived from the supplied evidence." if has_evidence else None,
        "workflow": "Derived from the supplied evidence." if has_evidence else None,
        # A fake cannot judge thesis fit, and a mismatch is a finding that would need
        # evidence. Undetermined is the only honest verdict it can reach.
        "thesis_fit": "undetermined",
        "sections": sections,
        "score_components": components,
        "open_questions": [],
        "recommendation_changers": [
            "Verified evidence of paying customers and retention.",
            "Founder background and prior experience in this workflow.",
        ],
        "model_suggested_recommendation": _suggestion(len(claim_ids)),
        "identity_warnings": [],
        "analysis_warnings": (
            []
            if has_evidence
            else [
                "No evidence claims were supplied, so no investment case could be assessed. "
                "This is a statement about the evidence, not about the company."
            ]
        ),
    }


def _suggestion(claims: int) -> str:
    """A deterministic advisory suggestion, from evidence volume alone.

    Deliberately not the policy's own rule, so an offline run still exercises the
    model-versus-policy comparison instead of agreeing by construction.
    """
    if claims == 0:
        return "pass"
    return "take_a_meeting" if claims >= 6 else "watch"
