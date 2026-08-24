# Worklog 08 - Assessment calibration (analysis_v2)

**Stage:** a narrow Stage 5 calibration before the demo run. No provider call, no network,
no artifact regenerated, no stored analysis rewritten.

## The defect

The audited live run graded **0 of 105** component slots `supported`: 26
`partially_supported`, 3 `contradicted`, 76 `not_assessable`. Under the status ceilings
that capped the achievable total at 63 and made the take-a-meeting band arithmetically
unreachable for all fifteen candidates *before any judgement about the companies*.

The cause was not the rubric, the ceilings or the policy. It was `analysis_v1` reading
"company-authored" as "cannot support anything", which collapsed two different questions
into one.

## The semantic change, in one sentence

`assessment_status` now answers **how directly the cited evidence supports the conclusion
being drawn**, and provenance is no longer a blanket prohibition on `supported` - it stays
recorded per claim, feeds research confidence, and caps a specific *kind* of conclusion
rather than all of them.

The line: a company's own page is good evidence of **what the product is** - what it does,
who buys it, what it integrates with, what it costs, who works there. It is not evidence
of a **result, an advantage or a scale** - savings, ROI, moats, adoption, customer counts,
revenue, market size. Those stay at most `partially_supported` until another voice carries
them.

## What was built

- `src/vc_scout/assessment_policy.py` - the versioned table (`assessment_v1`): per
  dimension, what an explicit company source may support and what stays capped. It is
  **rendered into the user message** beside the rubric rather than written into the prompt
  file, so the rule the model is given and the rule the validator enforces have one
  definition and cannot drift.
- `src/vc_scout/prompts/analysis_v2.md` - rewritten "how to read the labels" and a new
  "what each status means" section, with ten worked positive/negative examples. `v1` stays
  on disk; a prompt version is never edited in place.
- `src/vc_scout/llm/analysis_validation.py` - `support_evidence()` and
  `check_supported_rating()`, applied to any component the model rates `supported`.
- `tests/unit/test_assessment_policy.py` - the evaluation matrix, 32 cases.

Unchanged, deliberately: the 100-point rubric, the status ceilings, the score bands, the
confidence formula, `POLICY_VERSION` (2.0.0) and every guardrail. No deterministic decision
rule changed, so no policy version moved. The provider schema is untouched at **1,829 of
2,400 bytes**.

## What is mechanically enforced, and what is not

Two rules, both decidable without reading meaning:

1. **A result on the company's word alone cannot be `supported`.** For `pain_roi`,
   `traction` and `market_timing`, if every cited claim is `company_claim` and the
   rationale asserts a percentage improvement, a revenue or retention metric, a customer
   count or a market-size figure, `supported` is refused with the reason.
2. **A rating over a recorded conflict cannot be `supported` silently.** If a cited claim
   draws on a source the dossier records a conflict over, `supported` needs a caveat
   naming it.

Deliberately **not** enforced: whether a rationale genuinely follows from its evidence, and
whether "revolutionary AI platform" is concrete. A keyword checker for marketing adjectives
would pass any rationale that avoided the word and would manufacture more false confidence
than it prevented. That line lives in the policy table and the prompt examples, where a
human reviewer can see it, and the memo and site already show a reader the provenance of
every citation.

Also not enforced, by design: nothing reads `verification_status` to *grant* a status. The
`independently_supported` label is mechanical - the first live run produced one that was
two sources supporting different halves of a compound statement - and a test asserts that
relabelling a dossier changes no validated outcome.

The quantitative detector is narrow on purpose. It fires on a figure bound to an outcome
word, never on a bare number: "191 structured requirements", "42 points and 13 comments",
"$29 per seat", "11 years in banking" and "the EU AI Act took effect in 2026" all pass
untouched, because flagging them would recreate the defect this change exists to undo.

## Offline evaluation of the 15 stored dossiers

Read-only. No analysis was regenerated and no stored file was modified. Two estimates, both
computed from the existing claims and the existing recorded statuses.

**Upper bound** assumes any dimension with at least one eligible supporting claim could
reach `supported` and take the full maximum. **Same judgement, uncapped** keeps the model's
own score as a fraction of the range it was allowed and re-measures it against the full
maximum - what the recorded reasoning would have produced without the cap.

| candidate | claims | score now | old ceiling | same judgement, uncapped | upper bound |
|---|---:|---:|---:|---:|---:|
| rulemesh-com | 11 | 40 | 63 | **63** | 100 |
| simplai | 10 | 26 | 53 | 51 | 100 |
| heydeacon | 8 | 31 | 55 | 51 | 92 |
| recursive | 5 | 27 | 53 | 47 | 92 |
| ticketdesk-ai | 7 | 30 | 60 | 44 | 86 |
| budibase-agents-beta | 10 | 25 | 56 | 42 | 92 |
| clerk | 7 | 26 | 60 | 39 | 95 |
| palmier | 8 | 19 | 50 | 32 | 87 |
| dooza-desk | 2 | 16 | 50 | 27 | 87 |
| agentic-commits | 6 | 17 | 56 | 26 | 87 |
| run | 2 | 16 | 53 | 25 | 87 |
| noworkflows-dev | 2 | 13 | 53 | 19 | 87 |
| realtechsolutions-work-gd | 1 | 14 | 50 | 16 | 61 |
| gibsonai-com | 0 | 14 | 48 | 14 | 48 |
| lumro | 0 | 14 | 48 | 14 | 48 |

**Which claims could support which dimensions.** Across 79 claims (65 `company_claim`, 13
`community_signal`, 1 `independently_supported`), the number of candidates with at least
one eligible supporter per dimension: distribution 13, traction 13, defensibility 12,
wedge 12, pain_roi 11, market_timing 6, **team 3**. Team is the thin one - only three
dossiers carry a claim about who works at the company at all. Five company-authored claims
assert a result or scale figure and stay capped under rule 1 (3 product, 1 market,
1 traction).

**Which candidates remain structurally unable to reach 80.** Three:
`gibsonai-com` and `lumro` (zero claims - nothing changes for them, correctly), and
`realtechsolutions-work-gd` (one claim, ceiling 61).

**Could any candidate plausibly reach 80 without inflation? No.** On the same judgement
uncapped, the highest total in the run is **rulemesh-com at 63** - still the pass band, 17
points short. The calibration removes a *structural* block; it does not manufacture a
meeting. That is the correct result: this run's evidence is genuinely thin, and a change
that turned it into a meeting would have been the inflation the brief forbids.

What does change materially: twelve candidates stop being arithmetically excluded, the
spread widens from 13-40 to roughly 14-63, and a partner can tell "we found little" from
"we found a lot and it was unconvincing" - which the old run could not express.

## Best single candidate for a controlled live verification

**`rulemesh-com`.** It has the most claims (11) across five categories, the only high
research confidence in the run (0.66), a real website with four extracted pages, zero
conflicts and zero identity warnings, the highest current score (40) and the highest
uncapped estimate (63). It is the one candidate where all seven dimensions have an eligible
supporter, so a single request exercises every row of the new policy table - and its
`distribution` and `market_timing` are currently `not_assessable`, which is exactly what
should be re-examined.

```
uv run vc-scout analyze --run-id source-test --company-id rulemesh-com --force
```

One request. Every other analysis is left untouched and the report records `filtered_to`.

## Verification

```
uv run pytest                  872 passed (32 new)
uv run ruff check .            clean
uv run ruff format --check .   101 files already formatted
uv run mypy src                clean, 52 source files
git diff --check               clean
```

No provider call was made. No artifact under `outputs/runs/` was read for anything but this
audit, and none was written.

## Remaining risks

- **The upper-bound column is optimistic and should not be quoted on its own.** It assumes
  one eligible claim can carry a dimension to full marks, which is a ceiling, not a
  forecast. The uncapped column is the honest estimate.
- **The correction could overshoot.** Telling a model that company sources may support
  concrete facts risks it treating fluent marketing copy as concrete. Rule 1 catches the
  numeric form of that; the adjective form is caught only by the prompt and by a reader.
  The next live run should be checked for `supported` ratings whose rationale is
  paraphrased marketing.
- **The detector is a regex.** It will miss phrasings it has not seen ("north of a thousand
  seats") and it operates on the rationale, not on the underlying claim. It is a floor, not
  a filter.
- **`team` has almost no evidence in this run** (3 of 15 dossiers), so the team dimension
  will stay `not_assessable` for most candidates regardless of this change. That is an
  evidence-gathering problem in Stage 3/4, not a scoring one.
- **This is unverified against a live model.** Everything above is offline reasoning about
  what the policy permits. The single-candidate run named above is the next step, and its
  result may show the prompt needs another pass.

## Personal observations

TODO(author): whether the line drawn here - product facts supportable, results and scale
not - is where you would draw it, or whether you would let a company source carry more or
less than this.

TODO(author): whether `traction` should treat Hacker News points as `supported` freshness
at all, given how weak a signal engagement turned out to be across this run.

TODO(author): whether an uncapped rulemesh-com at roughly 63 reads right to you against
the memo, or whether the evidence still feels like a 40.

TODO(author): whether to re-run the whole 15 under analysis_v2 for the demo, or to keep the
audited v1 run as the honest record and show the calibration as a documented next step.
