# Worklog 05 - Evidence-bound analysis, scoring and recommendation

**Stage:** analysis only. `uv run vc-scout analyze --run-id <run-id>`
**Scope as given:** read the candidates, the evidence dossiers and the evidence report;
score seven rubric dimensions against a versioned thesis; compute research confidence
deterministically; apply a deterministic recommendation policy with guardrails; persist
per-attempt request and response artifacts. Explicitly excluded: Markdown memos, ranking
page, static UI, another provider, database, queue, concurrency, prompt caching.

## What was built

- `src/vc_scout/thesis.py` - the thesis as versioned configuration (`thesis_v1`) with a
  content hash recorded in every artifact.
- `src/vc_scout/prompts/analysis_v1.md` - the versioned runtime prompt.
- `src/vc_scout/llm/analysis_schema.py` - the forced-tool JSON schema. It deliberately does
  not contain `total_score` or `research_confidence`: both are derived after the call, so
  the model is never asked for a value it should not decide.
- `src/vc_scout/llm/analysis_validation.py` - validation of model output against the
  dossier, including the market-size scrubber and deterministic reference normalisation.
- `src/vc_scout/models/analysis.py` - rewritten: `ScoreComponent`, `AnalysisSection`,
  `RiskItem`, `CompetitiveObservation`, `CorroboratedFinding`, `ThesisAssessment`,
  `StartupAnalysis`.
- `src/vc_scout/policy.py` - rewritten: `compute_confidence` and `decide` with the
  guardrails. Policy version moved to `2.0.0`.
- `src/vc_scout/stages/analysis.py` - the stage.
- `src/vc_scout/models/enums.py` - `AssessmentStatus`, `ThesisFit`, and the four new LLM
  error categories.
- `src/vc_scout/util/ids.py` - `unknown_id_for`, so recorded unknowns can be cited without
  changing the persisted evidence schema.
- `src/vc_scout/models/report.py` - `AnalysisReport`, `AnalysisOutcome`, `AnalysisAttempt`.
- `src/vc_scout/store.py` - analysis request/response paths, `analysis_company_ids`,
  `delete_analysis`.
- `src/vc_scout/cli.py` - `analyze` now runs scoring; `--evidence-only` behaviour unchanged.

## Input contract

The stage reads `candidates.json`, `evidence/<company_id>.json` and (for reporting)
`evidence-report.json`, plus the fixed thesis and rubric. Raw pages, raw Hacker News
responses and the web are unreachable from it, and a test asserts that no raw-artifact
marker appears in any prompt. No second evidence-extraction call is made.

Unknowns are persisted without identifiers, so analysis derives one from the question text
(`unk-<sha256(company_id, question)[:12]>`) when it renders the dossier, and validates
references against the same derivation. The Stage 4 evidence schema was not changed.

## Scoring and its ceilings

Seven components, each with the rubric's configured maximum, an assessment status, a
rationale, evidence claim IDs, relevant unknowns and caveats. Ceilings are floored:

| Status | Ratio | pain_roi (20) | 15-point dimensions | 10-point dimensions |
| --- | ---: | ---: | ---: | ---: |
| `supported` | 1.00 | 20 | 15 | 10 |
| `partially_supported` | 0.70 | 14 | 10 | 7 |
| `contradicted` | 1.00 | 20 | 15 | 10 |
| `not_assessable` | 0.50 | 10 | 7 | 5 |

`not_assessable` is neither forced to zero nor to the midpoint; the model chooses within the
cap and must explain the choice. `supported` and `partially_supported` require at least one
evidence claim ID; `contradicted` requires the contrary evidence.

## Research confidence

Computed after the model answers, from countable coverage facts. The formula and thresholds
are documented in `docs/DECISIONS.md` D30 and reproduced in the `compute_confidence`
docstring. A zero-claim dossier scores 0.0 outright.

`independently_supported` earns nothing by itself; only `corroborated_findings` that the
analysis explicitly records count towards the corroboration component. This follows directly
from the Stage 4 finding that one such label covered two sources supporting different halves
of a compound statement.

## Recommendation policy

Bands 80/65 as specified, then six guardrails applied in a fixed order: zero-claim dossier,
insufficient-evidence watch, the four meeting requirements, and the identity cap. Every
guardrail that fires is recorded by name alongside the band it moved from, the model's
suggestion and whether the two disagreed.

The identity cap is evaluated against the *band* rather than the running decision. Without
that, an identity warning that had already lowered confidence would trigger the confidence
guardrail first and the identity constraint would never appear in the record - found by a
test, and the outcome was correct while the audit trail was not.

## Objective notes from implementation

The meeting guardrail requiring evidence in four dimensions cannot fire through the normal
path: with only three evidenced dimensions the status ceilings cap the achievable total at
74, below the 80 threshold. It is retained as defence in depth and would catch a future
change to the ceilings. A test computes that 74 from the rubric rather than asserting it, so
the claim stays true if the rubric changes.

Rewriting the analysis models and policy broke 31 pre-existing tests in `test_models.py`,
`test_policy.py`, `test_store.py` and `test_cli.py`. All were updated to the new contract;
`test_policy.py` was rewritten, and analysis builders moved to a new
`tests/unit/analysis_fixtures.py`.

`ScoreComponent` validation reports a different message when the status ceiling equals the
dimension maximum, because the maximum check fires first. Both messages are correct and the
test accepts either.

No live Stage 5 provider call was made.

## Test coverage added

| Guarantee | Test |
| --- | --- |
| Seven-component valid analysis, every maximum, total calculation | `test_a_well_formed_analysis_validates`, `test_every_component_carries_its_configured_maximum`, `test_the_total_is_recomputed_from_the_components` |
| All four assessment states | `test_every_assessment_status_is_accepted` |
| Status-specific ceilings | `test_status_ceilings_are_enforced`, `test_status_ceilings_are_floored_not_rounded_up` |
| `not_assessable` neither zeroed nor midpointed | `test_not_assessable_is_neither_forced_to_zero_nor_to_the_midpoint` |
| Unknown evidence ID / unknown reference rejection | `test_an_unknown_evidence_claim_id_is_rejected`, `test_an_unknown_unknown_reference_is_rejected` |
| Duplicate and missing components | `test_a_duplicated_rubric_dimension_is_rejected`, `test_a_missing_rubric_dimension_is_rejected` |
| Duplicate references normalised | `test_duplicate_references_are_normalised_deterministically` |
| Unsupported market-size rejection | `test_unsupported_market_size_figures_are_caught`, `test_an_invented_market_size_rejects_the_analysis` |
| Exactly two or three changers | `test_the_wrong_number_of_recommendation_changers_is_rejected` |
| Deterministic confidence, thresholds, company-claim-heavy, zero-claim, conflicts, identity | `test_confidence_is_deterministic`, `test_broad_third_party_evidence_yields_high_confidence`, `test_a_company_claim_heavy_dossier_scores_lower_than_a_mixed_one`, `test_a_zero_claim_dossier_scores_zero_confidence`, `test_conflicts_reduce_confidence`, `test_identity_warnings_reduce_confidence` |
| Take-a-meeting guardrails | `test_a_meeting_requires_at_least_medium_confidence`, `test_a_meeting_requires_an_identifiable_buyer`, `test_a_meeting_needs_evidence_in_four_dimensions_and_four_is_enough` |
| Low-confidence insufficient-evidence watch | `test_a_low_score_driven_by_missing_evidence_becomes_watch_not_pass` |
| Missing website does not force pass | `test_a_missing_website_alone_never_forces_pass`, `test_a_missing_website_does_not_force_a_pass` |
| Supported thesis mismatch may pass | `test_a_supported_thesis_mismatch_may_pass_even_at_low_confidence` |
| Zero-claim dossier becomes watch | `test_a_zero_claim_dossier_becomes_watch_rather_than_a_fabricated_narrative` |
| Identity mismatch caps at watch | `test_an_identity_warning_caps_the_recommendation_at_watch` |
| Model/policy disagreement preserved | `test_the_model_suggestion_never_overrides_the_policy`, `test_the_decision_is_identical_for_every_possible_model_suggestion` |
| Independent-support label gives no automatic credit | `test_the_independently_supported_label_earns_no_confidence_by_itself`, `test_the_independently_supported_label_alone_earns_no_confidence` |
| Conflict remains visible | `test_a_conflict_in_the_dossier_reaches_the_model_intact` |
| Prompt injection cannot alter rubric or policy | `test_injected_instructions_are_supplied_only_as_fenced_dossier_content`, `test_an_obeyed_injection_still_cannot_break_the_rubric_or_the_policy`, `test_an_injection_cannot_force_a_recommendation_through_the_policy` |
| One retry, exactly one, permanent failure | `test_invalid_output_is_retried_exactly_once_and_can_succeed`, `test_two_invalid_attempts_are_a_permanent_failure_and_never_a_third` |
| Failure isolation and stale-analysis cleanup | `test_one_failing_candidate_does_not_fail_the_run`, `test_a_failed_candidate_does_not_retain_a_stale_analysis`, `test_cleanup_never_touches_another_candidates_analysis` |
| Request and response artifact safety | `test_request_artifacts_record_the_bounded_input_and_the_rubric`, `test_persisted_artifacts_contain_no_keys_headers_or_absolute_paths` |
| `--evidence-only` unchanged | `test_evidence_only_mode_is_unaffected_by_the_scoring_mode` |
| No network access | the autouse guard in `tests/conftest.py`, plus `FakeProvider` throughout |

## Verification performed

| Command | Result |
| --- | --- |
| `uv run pytest` | 620 passed |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | all files formatted |
| `uv run mypy src` | no issues in 41 source files |
| `git diff --check` | clean |

## Notes for the next stage

- `analyses/<company_id>.json` holds two top-level keys, `analysis` and `recommendation`.
- A candidate that failed analysis has no file and appears in `analysis-report.json` with
  its error category. The memo stage must treat that as missing analysis, not as a pass.
- `verification_status`, `assessment_status` and `scored_out_of` are the three things a memo
  must surface. A company claim rendered as verified fact, or a low total shown without the
  assessable denominator, would defeat the whole chain.
- `RecommendationResult.guardrails_applied` is what the memo's "why this call" section
  should be built from.

## First live run: failed before inference

The first live Stage 5 run failed for all fifteen candidates with HTTP 400
`invalid_request_error`, zero input and output tokens, no retries. The request was rejected
before any inference happened.

Diagnosis was performed entirely offline from the persisted artifacts:

- All fifteen attempts carried an identical detail, and 400 is correctly non-retryable, so
  the stage made exactly one attempt each.
- The same credential, model (`claude-sonnet-5`), effort (`low`) and endpoint had completed
  Stage 4 minutes earlier - ruling out authentication, account state, rate limiting, and
  model or effort incompatibility.
- The largest Stage 5 payload was 7,221 characters, against 1,998 for Stage 4. Both are far
  below any request size limit.
- The request envelope is built by one shared method that differs between stages only in the
  tool name and the schema, and the messages array is constructed identically.
- Comparing the two schemas construct by construct left exactly one difference: the analysis
schema contained an `enum` whose values included `null`. That was recorded as the probable
cause and flagged at the time as an inference rather than a proven one, because the provider
had discarded the API's own message.

**That hypothesis was disproven.** See "Second live run" below.

Three changes were made, recorded as D34 (now marked disproven), D35 and D36.

1. **The schema construct.** `model_suggested_recommendation` is now a plain string enum,
   left out of `required` so it can be omitted. A test asserts the analysis schema
   introduces no construct the evidence schema has not already proved in a live run.
2. **The discarded diagnostic.** The provider recorded only the status and error type, so no
   artifact named the offending field. It now records the API's own message, bounded to 400
   characters, whitespace-collapsed and scrubbed of key-shaped text. The request content is
   already persisted alongside, so the message discloses nothing new.
3. **No fail-fast.** The stage issued fifteen identical doomed requests. `LlmError` now
   carries `run_level`, set for 400/401/403/404 and a missing credential; a run-level failure
   stops the run, records every remaining candidate as not attempted with the reason, and
   clears any artifact left from an earlier run for them too. Transient failures still retry
   within the candidate, and candidate-specific failures (413, malformed response, validation)
   still allow the run to continue. The same fix was applied to the evidence stage, which
   shared the loop and the defect.

The diagnosis of change 1 was a strong inference rather than a proven cause. Change 2 is
what made the next attempt a diagnosis instead of another guess - and it showed the
inference was wrong.

The failed live run also confirmed the stale-artifact cleanup working as intended: it removed
all fifteen fake-provider analyses (`stale_analyses_removed: 15`), leaving `analyses/` empty,
so no fake output can be mistaken for live output.

## Second live run: the established root cause

The controlled re-run, with the provider now recording the API's own message, returned:

> HTTP 400 `invalid_request_error`: The compiled grammar is too large, which would cause
> performance issues. Simplify your tool schemas or reduce the number of strict tools.

Fail-fast worked: only `recursive` was attempted, and the remaining fourteen candidates were
never sent.

This **disproves the enum-with-null hypothesis** recorded above and in D34. The rejection was
about the size of the compiled grammar, not about any single construct; the null-bearing enum
was a coincidence of the diff. D34 is marked disproven rather than deleted, and D37 records
the established cause and the fix.

### What was changed

The provider-facing schema was separated from the analysis contract and compacted. Measured:

| Metric | Before | After | Evidence tool (compiles) |
| --- | ---: | ---: | ---: |
| Serialized bytes | 6,034 | **1,829** | 2,745 |
| Distinct object shapes | 9 | **3** | 6 |
| Description characters | 2,086 | **0** | 922 |

Four simplifications:

1. **One shape for every grounded statement.** The three narrative sections, the thesis
   rationale, the risks, the competitive observations and the corroborated findings were six
   distinct object definitions. They are now one `sections` array whose items carry a `kind`,
   which the validator partitions back out.
2. **All descriptions removed.** Every instruction they carried is in the versioned prompt.
3. **`maximum` dropped from the component shape.** It is a rubric constant; the validator
   fills it from configuration.
4. **`thesis_assessment` flattened** into a top-level `thesis_fit` enum plus a `thesis`
   section.

Nothing was deleted to fit: every field the persisted `StartupAnalysis` carries is still
expressible, and a test asserts the exact property set plus all seven section kinds.

### What did not change

Exactly one forced strict tool, `strict: true`, `tool_choice` pinned, parallel tool use
disabled. No free-form JSON, no prose parsing, no splitting a candidate across calls. Every
vocabulary is still enum-constrained in the schema. All seven components, the rubric maxima,
the status ceilings, the recomputed total, reference integrity, grounding, the
competitor-naming rule, the market-size scrubber and the two-or-three changers are enforced
by the local validator, which was always the authority and is unchanged.

### Single-candidate runs

`analyze --company-id <id>` was added so the next live verification costs one request rather
than fifteen. It analyses only that candidate, leaves every other analysis untouched, records
`filtered_to` in the report, and rejects an unknown ID before any provider call.

## Corrective work after the acceptance audit

The offline acceptance audit returned FIX with two blocking defects. Both were fixed without
re-running inference; no provider call was made for any of the work below.

### Defect 1 - a rationale that quoted a zero denominator

For a candidate whose every dimension came back `not_assessable`, `scored_out_of` is 0, and
the policy still wrote "Only 0 of 100 points were assessable" beside a positive total. The
sentence is what a memo renders, and read literally it is nonsense. That branch now says:

> No dimension could be assessed from the available evidence: <dimensions>. The points shown
> reflect residual uncertainty, not established merit.

The partially-assessable wording is untouched, and so are the scores, the bands, the
confidence, the guardrails and the recommendations. A regression test scans every rationale
line for a total quoted against a smaller denominator, so the class of defect - not just this
sentence - is covered.

### Defect 2 - stale attempt files outliving the run that wrote them

`llm/` held 30 request and 30 response files against 20 recorded attempts. The extra ten were
`attempt2` pairs from an earlier failing run, left in place because a later run needed only
one attempt and never overwrote them. They recorded failures for candidates the report says
succeeded first try. This is not cosmetic: the audit script written to check the run read
those files and produced a false FAIL, which is exactly the confusion a reviewer would hit.

`RunStore.delete_llm_attempts(company_id, stage=...)` now removes one candidate's prior
attempt files immediately before that candidate is processed, in both the evidence and the
analysis stage. It validates the company ID, resolves inside the run directory, globs only
`<company_id>-attempt*.json` under `llm/<stage>-requests` and `llm/<stage>-responses`, and
touches no dossier, analysis, extracted page or source artifact. Candidates that a run-level
abort left unattempted are cleaned too, so a failed run does not leave a previous run's
attempts standing as if they were its own. The count is reported as
`stale_attempts_removed`. After a run, a candidate's attempt files on disk are exactly the
attempts the report records for it, for filtered and full runs alike.

## What the live scores actually showed

Across 15 analysed candidates and 105 component slots, `supported` was used **zero** times:
76 slots came back `not_assessable`, 26 `partially_supported` and 3 `contradicted`. Under the
rubric ceilings that caps an all-partial analysis at 68, and the actual mix here capped every
candidate lower still - the highest headroom any candidate reached was 63. The take-a-meeting
band at 80 was **arithmetically unreachable for every candidate in the run**, before any
judgement about the companies themselves.

That is a property of the evidence available to the model, not a verdict on the companies,
and the report did not say so. Each `AnalysisOutcome` now carries `maximum_achievable_score`
and `meeting_reachable_by_statuses`, computed deterministically from the recorded statuses
and the rubric ceilings. They are report metadata: they are derived after scoring, they are
not inputs to the score, the confidence or the recommendation, and a test asserts the policy
record contains neither field. The CLI shows `max=NNN` per row, a trailing `-` where the band
was out of reach, and one summary line counting the affected candidates.

The underlying cause is upstream: nearly all evidence in this run is company-authored, and
the evidence contract labels company-authored material `company_claim`, which the analysis
prompt treats as weaker than independent corroboration.

TODO(author): decide whether company-authored evidence should ever qualify as `supported`.
Never doing so means a company with only its own website can never reach a meeting, however
good it is; sometimes doing so weakens what `supported` means. This is a thesis question, not
a code question, and it is deliberately left open here.

## Retry behaviour after the schema compaction

3 of the 5 live retries in the run were near-empty first tool responses - the model returned
the tool call with almost no content, and the second attempt succeeded. The other 2 were
ordinary validation failures. The pattern appeared only after the provider schema was
compacted, so the compaction is the obvious suspect, but 3 observations across one run is not
enough to attribute it, and changing the prompt now would make the next run's numbers
uncomparable. **The prompt is deliberately unchanged.** The next live run should be checked
for the same pattern before anything is altered.

## Personal observations

TODO(author): your read on whether the status ceilings land in the right place once you have
seen live scores - in particular whether 50% for `not_assessable` is too generous.

TODO(author): whether the insufficient-evidence guardrail fires more often than is useful on
the live run, and whether "watch" is the right destination for it.

TODO(author): whether the confidence weights produce a spread you would act on, or whether
most candidates cluster in one band.
