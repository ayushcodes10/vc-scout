"""What a source may be used to support, per rubric dimension.

The first live run graded **zero** of 105 component slots `supported`. Every dimension of
every candidate came back `partially_supported`, `contradicted` or `not_assessable`, which
under the status ceilings capped the achievable total at 63 and made the take-a-meeting
band arithmetically unreachable for all fifteen - before any judgement about the companies.
The cause was a prompt that read "company-authored" as "cannot support anything".

That conflated two different questions, and this module separates them:

* ``assessment_status`` - **how directly the cited evidence supports the rubric
  conclusion.** A company's own page is perfectly good evidence that its product
  integrates with Jira. It is not evidence that the integration is a moat.
* ``verification_status`` - **who said it**, recorded per claim and carried through to the
  memo and the site.
* ``research_confidence`` - how much the run established overall, computed from coverage.
* the **recommendation guardrails** - the deterministic decision safety net.

Provenance therefore stops being a blanket prohibition on `supported` and becomes what it
always was: one input a reader can see. What stays capped is the *kind of conclusion* -
existence and description may be supported from a company source; performance, advantage
and scale may not, without corroboration.

Nothing here changes the rubric, the ceilings, the thresholds or the policy version. It
changes what the model is told a source can be used for, and adds the few checks on that
which are mechanically decidable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from vc_scout.models.enums import RubricDimension

__all__ = [
    "ASSESSMENT_POLICY",
    "ASSESSMENT_POLICY_VERSION",
    "GUARDED_QUANTITATIVE",
    "DimensionPolicy",
    "policy_for",
    "quantitative_outcome_terms",
    "render_policy",
]

#: Bumped whenever the table below changes. Recorded beside the prompt version, because a
#: change here changes what the model is allowed to conclude.
ASSESSMENT_POLICY_VERSION = "assessment_v1"


@dataclass(frozen=True, slots=True)
class DimensionPolicy:
    """What a single source may and may not establish for one rubric dimension."""

    key: RubricDimension
    #: Conclusions an explicit company-authored source may support outright.
    supportable: str
    #: Conclusions that stay at most ``partially_supported`` without corroboration.
    capped: str


ASSESSMENT_POLICY: tuple[DimensionPolicy, ...] = (
    DimensionPolicy(
        key=RubricDimension.PAIN_ROI,
        supportable=(
            "that a named workflow or problem is the one the product targets, and how the "
            "company frames its cost"
        ),
        capped=(
            "any claim of savings, revenue, speed, conversion or measurable ROI - a "
            "self-reported number is evidence of the claim, not of the result"
        ),
    ),
    DimensionPolicy(
        key=RubricDimension.WEDGE,
        supportable=(
            "concrete facts about what the product does, its buyer, the workflow it enters, "
            "its pricing, its integrations and its delivery model"
        ),
        capped=(
            "marketing adjectives with no concrete detail - 'revolutionary', 'AI-powered', "
            "'end-to-end' establish nothing on their own"
        ),
    ),
    DimensionPolicy(
        key=RubricDimension.DISTRIBUTION,
        supportable=(
            "explicit self-serve availability, a named channel, the integration surface or "
            "a stated go-to-market motion, as facts about how the product is sold"
        ),
        capped=(
            "any claim of distribution advantage, adoption or repeatability, which needs "
            "corroboration beyond the company's own description"
        ),
    ),
    DimensionPolicy(
        key=RubricDimension.DEFENSIBILITY,
        supportable=(
            "the existence of concrete proprietary workflow depth, integrations or "
            "operational features that the sources describe in specifics"
        ),
        capped=(
            "the claim that any of it constitutes a moat or a durable advantage, which is "
            "a judgement the company's own page cannot settle"
        ),
    ),
    DimensionPolicy(
        key=RubricDimension.TEAM,
        supportable=(
            "factual team composition - named roles, biographies and technical backgrounds "
            "the sources state"
        ),
        capped=(
            "quality judgements, prior exits and claims of exceptional depth, which stay "
            "partial while only the team says them"
        ),
    ),
    DimensionPolicy(
        key=RubricDimension.TRACTION,
        supportable=(
            "launch date, Hacker News points and comments, and observable public repository "
            "activity, as evidence of freshness and community attention"
        ),
        capped=(
            "customer count, revenue, retention, growth and adoption, which stay partial "
            "unless independently corroborated; a recorded conflict about traction stays "
            "contradicted or explicitly caveated"
        ),
    ),
    DimensionPolicy(
        key=RubricDimension.MARKET_TIMING,
        supportable=(
            "a concrete regulatory, technical or workflow change that the evidence names, "
            "as the reason this is buildable or buyable now"
        ),
        capped=(
            "market size and broad demand claims, which stay partial without independent evidence"
        ),
    ),
)

_BY_KEY: dict[RubricDimension, DimensionPolicy] = {item.key: item for item in ASSESSMENT_POLICY}

# The table must cover every dimension exactly once; a gap would silently leave a dimension
# with no stated policy while the prompt implied there was one.
assert set(_BY_KEY) == set(RubricDimension)
assert len(ASSESSMENT_POLICY) == len(RubricDimension)


def policy_for(dimension: RubricDimension) -> DimensionPolicy:
    return _BY_KEY[dimension]


#: Dimensions where a figure in the rationale is an *outcome* claim rather than a
#: description. A count of integrations is a fact about the product; "saves 80%" and
#: "hundreds of customers" are results, and results need a voice other than the company's.
GUARDED_QUANTITATIVE: frozenset[RubricDimension] = frozenset(
    {RubricDimension.PAIN_ROI, RubricDimension.TRACTION, RubricDimension.MARKET_TIMING}
)

#: Words that make a figure a *result* rather than a description.
_OUTCOME_WORDS = (
    r"\b(?:sav\w*|reduc\w*|cut|cuts|faster|increase\w*|improv\w*|growth|grew|"
    r"conversion|retention|resolv\w*|automat\w*|cheaper|uplift|accuracy|efficien\w*)"
)

#: Deliberately narrow. Each pattern is a figure *attached to* an outcome word, not a bare
#: number: a checker that fires on any digit would push honest, concrete rationales down a
#: band, which is the failure this whole change exists to undo. A published price is a fact
#: about the product, so a bare money figure is not on this list - only money bound to a
#: revenue metric or a market size is.
_OUTCOME_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "a percentage improvement",
        re.compile(
            _OUTCOME_WORDS + r"\b[^.]{0,60}?\d+\s?%" r"|\d+\s?%[^.]{0,60}?" + _OUTCOME_WORDS,
            re.IGNORECASE,
        ),
    ),
    (
        "a revenue or retention metric",
        re.compile(
            r"\b(?:arr|mrr|revenue|churn|retention|ltv|cac|payback)\b[^.]{0,40}?\d"
            r"|\d[^.]{0,40}?\b(?:arr|mrr|revenue|churn|retention|ltv|cac|payback)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "a customer or user count",
        re.compile(
            r"\b(?:\d[\d,.]*|dozens|hundreds|thousands|millions)\s+(?:of\s+)?"
            r"(?:paying\s+|active\s+|enterprise\s+)?"
            r"(?:customers?|users?|clients?|teams?|companies|businesses|subscribers?|seats?)\b"
            r"|\b(?:customer|user|client|team|seat)\s+(?:count|base)\b[^.]{0,20}?\d",
            re.IGNORECASE,
        ),
    ),
    (
        "a market-size figure",
        re.compile(
            r"\b(?:tam|sam|som|addressable market|market size|market of)\b[^.]{0,40}?\d"
            r"|\d[\d,.]*\s*(?:million|billion|trillion)\b[^.]{0,40}?\bmarket\b",
            re.IGNORECASE,
        ),
    ),
)


def quantitative_outcome_terms(text: str) -> list[str]:
    """The outcome-shaped figures ``text`` asserts, by kind.

    Used to decide whether a `supported` rating rests on a *result* that only the company
    has stated. It answers "is this a performance claim?", never "is this true?".
    """
    return sorted({label for label, pattern in _OUTCOME_PATTERNS if pattern.search(text)})


def render_policy() -> list[str]:
    """The policy table as prompt lines.

    Rendered into the user message beside the rubric rather than written into the prompt
    file, so the table has exactly one definition and the prompt cannot drift from the code
    the validator enforces.
    """
    lines = [f"## Source-to-assessment policy ({ASSESSMENT_POLICY_VERSION})"]
    for item in ASSESSMENT_POLICY:
        lines.append(f"- {item.key.value}:")
        lines.append(f"    may be supported by explicit company evidence: {item.supportable}")
        lines.append(f"    at most partially_supported without corroboration: {item.capped}")
    return lines
