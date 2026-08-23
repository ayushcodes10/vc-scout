# Worklog 04 - Source-grounded evidence extraction

**Stage:** evidence extraction only. `uv run vc-scout analyze --run-id <run-id> --evidence-only`
**Scope as given:** one production provider (Anthropic Messages API via httpx, no SDK), a
deterministic fake provider for tests, a provider-neutral interface, a versioned prompt,
a validated evidence dossier per candidate, one retry on invalid output, replayable request
and response artifacts, and prompt-injection defence. Explicitly excluded: investment
scoring, recommendations, memos, UI, a second production provider.

## What was built

- `src/vc_scout/llm/provider.py` - the provider-neutral interface: `LlmRequest`,
  `LlmResult`, `ModelConfig`, `LlmProvider`, `LlmError`. No credential is accepted as an
  argument, stored on an object, returned or logged.
- `src/vc_scout/llm/anthropic.py` - `AnthropicProvider`. Direct httpx against
  `POST /v1/messages`. Structured output via forced tool use: one tool carrying the schema,
  `tool_choice` pinned to it, `disable_parallel_tool_use`, `strict: true`. Returns parsed
  content, provider, model, input/output tokens, request ID, stop reason, latency, attempt.
- `src/vc_scout/llm/fake.py` - `FakeProvider`. Scripted responses or a handler, records
  every request, makes no network call and reads no credential.
- `src/vc_scout/llm/schema.py` - the tool's JSON schema, hand-written because strict mode
  rejects numeric and length constraints and requires `additionalProperties: false`
  throughout, and because the model must not be asked for a claim identifier.
- `src/vc_scout/llm/validation.py` - validation of model output against the supplied
  sources, and the derivation of claim IDs.
- `src/vc_scout/stages/evidence.py` - the stage: bounded input construction, the retry
  loop, artifact persistence and per-candidate isolation.
- `src/vc_scout/prompts/evidence_v1.md` + `prompts/__init__.py` - the versioned runtime
  prompt, loaded by version and content-hashed into every artifact.
- `src/vc_scout/models/evidence.py` - rewritten to the stage-4 contract.
- `src/vc_scout/models/enums.py` - `EvidenceCategory`, `VerificationStatus`,
  `InferenceStatus`, `LlmErrorCategory`.
- `src/vc_scout/models/report.py` - `EvidenceReport`, `EvidenceOutcome`, `EvidenceAttempt`.
- `src/vc_scout/store.py` - `llm_request_path`, `llm_response_path`,
  `evidence_report_path`, `evidence_company_ids`.
- `src/vc_scout/cli.py` - `analyze --evidence-only`, with `--provider`, `--model`,
  `--effort`, `--max-tokens` and a `--force` guard.

## API facts checked against the reference rather than recalled

The Anthropic API reference was consulted before writing the provider. Three findings
changed the implementation:

1. **Sampling parameters are rejected.** `temperature`, `top_p` and `top_k` return a 400 on
   current Claude models. The familiar "temperature 0 for determinism" is unavailable, so
   determinism is expressed through a versioned prompt, deterministic source ordering,
   bounded input and a fixed `effort` level instead. Recorded as D24.
2. **Strict schemas are a narrower dialect than JSON Schema.** `minimum`, `maximum`,
   `minLength` and `maxLength` are unsupported, and `additionalProperties: false` is
   required on every object. Excerpt length bounds therefore live in the validator.
3. **The request ID is a response header** (`request-id`), not a body field. The message
   `id` is used as a fallback.

## Evidence contract

Each `EvidenceClaim` carries a derived `claim_id`, a `category`
(`team`/`product`/`market`/`traction`/`risk`), the claim text, `source_ids`, one
`SupportingExcerpt` per cited source, a `verification_status`
(`company_claim`/`community_signal`/`independently_supported`), an `inference_status`
(`explicit`/`inferred`) and an optional `caveat`. The dossier adds `unknowns`, `conflicts`,
`source_coverage`, `confidence_inputs`, `warnings` and the provider, model and prompt
version used.

`confidence_inputs` is deliberately counts only. This stage reports what it saw; the
later policy computes confidence from it. Keeping them apart stops the two from merging
into one judgment.

## Validation and retry flow

1. Build the bounded per-candidate input: that candidate's HN record and extracted pages,
   in a deterministic order, capped at 6,000 characters per page and 18,000 per candidate,
   with truncation recorded.
2. Call the provider with the versioned system prompt and the forced tool.
3. Validate: unknown source IDs, missing or empty excerpts, excerpts absent from the source
   they are attached to, over-long excerpts, duplicate claims, invalid vocabularies, and
   `independently_supported` cited by fewer than two sources. All issues are collected
   before raising so the retry receives the whole list.
4. Derive claim IDs from claim content and assemble the dossier.
5. On failure, retry **once** with the same sources plus the validation errors. A second
   failure writes a structured candidate failure; the run continues.

## Prompt-injection controls

- System instructions come from the versioned prompt file and mention no company.
- Source text appears only in the user message, fenced in `BEGIN/END UNTRUSTED SOURCE <id>`
  markers and introduced as untrusted data.
- The prompt states explicitly that instructions found inside source text are page content,
  that they must not be followed, and that an attempt to issue them may be recorded as a
  quoted `risk` claim.
- The defence that is actually load-bearing is validation, not instruction: a model that
  obeyed an injected instruction to invent revenue would still have to produce an excerpt,
  and there is none, so nothing is written. That is what the tests assert.

## Objective notes from implementation

One defect was found by a test and fixed in the code rather than the test: the
"missing website evidence is not a negative signal" warning was being added by the stage,
so a caller that forgot to pass it would silently drop the distinction. It now lives in the
validator, where `website_available` is already known.

Rewriting `models/evidence.py` to the new contract broke eight pre-existing tests in
`test_models.py` and `test_store.py`. All eight were updated to the new shape; the shared
`tests/unit/factories.py` claim builder was rewritten.

`EvidenceReport` initially declared a `schema_version` field, which collided with the field
`ArtifactModel` already uses to version the artifact's own shape. Renamed to
`output_schema_version`.

No live Anthropic request was made during this stage.

## Test coverage added

| Guarantee | Test |
| --- | --- |
| Valid evidence extraction | `test_a_well_formed_claim_becomes_a_dossier`, `test_a_valid_response_becomes_a_persisted_dossier` |
| Company-claim labelling | `test_company_pages_are_labelled_as_company_claims` |
| HN community-signal labelling | `test_hacker_news_metrics_are_labelled_as_community_signals` |
| Unknown information | `test_unknowns_are_recorded_as_stated` |
| Missing website evidence | `test_missing_website_evidence_is_recorded_without_a_negative_claim`, `test_a_candidate_without_website_evidence_is_still_extracted` |
| Conflicting sources | `test_conflicting_sources_are_retained_rather_than_resolved` |
| Explicit versus inferred | `test_explicit_and_inferred_claims_are_distinguished` |
| Unknown source ID rejection | `test_an_unsupplied_source_id_is_rejected` |
| Missing excerpt rejection | `test_a_claim_without_any_excerpt_is_rejected`, `test_an_empty_excerpt_is_rejected` |
| Excerpt not found in source | `test_an_excerpt_absent_from_the_source_is_rejected` |
| Excerpt on the wrong source | `test_an_excerpt_attached_to_the_wrong_source_is_rejected_and_named` |
| Excessively long excerpt | `test_an_excessively_long_excerpt_is_rejected` |
| Duplicate claim IDs | `test_duplicate_claims_are_rejected` |
| Invalid independently-supported | `test_independently_supported_with_one_source_is_rejected` |
| Deterministic claim IDs | `test_claim_ids_are_derived_and_deterministic`, `test_a_model_supplied_claim_id_is_ignored` |
| One validation retry | `test_invalid_output_is_retried_exactly_once_and_can_succeed` |
| Exactly one retry, never more | `test_two_invalid_attempts_are_a_permanent_failure_and_never_a_third` |
| Successful second attempt | `test_the_retry_carries_the_validation_errors_and_the_same_sources` |
| Permanent failure after two attempts | `test_two_invalid_attempts_are_a_permanent_failure_and_never_a_third` |
| Candidate failure isolation | `test_one_failing_candidate_does_not_fail_the_run`, `test_an_unexpected_provider_fault_is_contained_to_one_candidate` |
| Prompt-injection source content | `test_injected_instructions_are_supplied_only_as_quoted_page_content`, `test_a_claim_invented_from_injected_text_cannot_be_persisted`, `test_an_injection_attempt_may_be_recorded_as_a_quoted_risk` |
| Request artifacts bounded and complete | `test_request_artifacts_record_the_bounded_content_supplied`, `test_source_text_is_bounded_and_truncation_is_recorded` |
| Response artifacts carry validation and usage | `test_response_artifacts_record_validation_and_usage`, `test_a_rejected_attempt_persists_its_validation_errors` |
| No keys, headers, secrets or absolute paths | `test_persisted_artifacts_contain_no_keys_headers_or_absolute_paths`, `test_the_persisted_body_carries_no_credential` |
| `--force` before overwrite | `test_analyze_requires_force_before_overwriting` |
| Suite cannot reach the network | `test_the_suite_genuinely_cannot_reach_the_network` (conftest guard), plus mocked transport throughout |

Additional coverage not on the required list: no sampling parameters are sent; the error
detail never echoes the provider's response body; a missing key issues no request at all;
another candidate's sources cannot leak into a prompt; no score, rubric or thesis term
reaches the model; a stored response can be re-validated without a provider.

## Verification performed

| Command | Result |
| --- | --- |
| `uv run pytest` | 431 passed |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | all files formatted |
| `uv run mypy src` | no issues in 37 source files |
| `git diff --check` | clean |

## Notes for the next stage

- `EvidenceDossier.confidence_inputs` carries the counts the confidence policy needs. It
  deliberately contains no score, band or judgment.
- A candidate with no dossier is recorded in `evidence-report.json` with its error
  category. The scoring stage must treat that as missing evidence, not as weakness.
- `verification_status` and `inference_status` are the labels the memo must surface; a
  company claim rendered as verified fact would defeat the whole chain.

## First live run

Executed against `anthropic` / `claude-sonnet-5` on the `source-test` run, prompt
`evidence_v1` (`ef10f8233af1`), schema `evidence-schema-1`.

- 15 candidates attempted; **14 produced live dossiers and 1 failed**.
- 73 claims, 68 unknowns, 1 conflict.
- 3 candidates retried; 18 attempts in total, never a third for any candidate.
- 102,951 input tokens, 23,782 output tokens across 17 attempts that reached the provider.
- Every figure in `evidence-report.json` reconciles against the persisted dossiers and the
  18 request and 18 response artifacts.

### Retries

| Candidate | Attempt-1 category | Outcome |
| --- | --- | --- |
| `dooza-desk` | `excerpt_not_found` (excerpt spanned a line break and lost a space) | second attempt passed |
| `ticketdesk-ai` | `provider_http_error` (`RemoteProtocolError`, 0 tokens) | second attempt passed |
| `budibase-agents-beta` | `excerpt_not_found` | second attempt failed identically |

### Why budibase failed

The source page contains `we’re launching Budibase Agents into Beta…` using `U+2019` RIGHT
SINGLE QUOTATION MARK. The model quoted it with an ASCII apostrophe (`U+0027`). Every other
character matched verbatim. Whitespace and NFKC normalisation do not touch `U+2019`, so the
excerpt was rejected, and with it a dossier of nine otherwise valid claims.

The retry could not recover because the validation error said only "Copy the excerpt
verbatim from the supplied text" and did not state what differed. A one-character
apostrophe difference is not visible at a glance, and the second attempt re-emitted the
same text.

13 of the 15 candidates had typographic punctuation in their supplied sources; only
`realtechsolutions-work-gd` and `run` had none. The run avoided further failures by chance.

### Stale artifact after the failure

`evidence/budibase-agents-beta.json` remained on disk from an earlier `--provider fake`
run (`provider: "fake"`, 0 claims, written three minutes before the live run began).
`write_evidence` runs only on success and `--force` did not clear the directory, so a
permanently failed candidate retained a dossier that a downstream stage would have read as
a successful extraction.

### Evidence quality audit

59 claims across seven candidates were re-verified independently against the persisted
request artifacts. All source IDs resolved, all excerpts matched their associated source,
and no claim cited a source outside its own dossier. Labelling was correct throughout:
company pages as `company_claim`, Hacker News metrics as `community_signal`. No invented
founders, funding, customers, revenue or market sizes were found. Caveats were applied to
unattributed testimonials, repeated same-voice claims and a company blog ranking itself.

`independently_supported` was used zero times across all 14 dossiers, and 1 of 73 claims
was marked `inferred`.

### GibsonAI

`gibsonai-com` returned a valid dossier with zero claims and four unknowns. The provider
declined to attribute the supplied pages to the candidate because four of five sources are
from `memorilabs.ai` - the candidate's website redirects across hosts - and recorded that
in a warning. Validation removed nothing; the empty claim list was deliberate.

This is preserved as an identity uncertainty rather than being forced into claims. Candidate
identity reconciliation after a cross-host redirect is a separate concern and was not
addressed in this stage.

### Missing-website candidates

`realtechsolutions-work-gd`, `dooza-desk` and `lumro` were extracted from Hacker News
material alone. Each records `website_available: false`, carries the structural warning that
missing evidence is not evidence of weakness, and produced five unknowns spanning every
category. No negative business claim was assigned to any of them, and no source from another
candidate appeared in any dossier.

## Fixes applied after the live audit

Two defects were corrected. Neither changes a persisted schema.

1. **Stale dossier after failure** (D25b). When a candidate finishes without a valid
   dossier, `RunStore.delete_evidence` removes any pre-existing dossier at that candidate's
   validated path before the report is written. The candidate remains in
   `evidence-report.json` with `succeeded=false`, its failure category, both attempts and
   their validation errors. Other candidates are untouched, and a missing file is not an
   error.
2. **Typographic punctuation** (D25, D25a). `normalize_for_match` now applies a closed fold
   of seven characters - `U+2018`, `U+2019`, `U+201C`, `U+201D`, `U+2013`, `U+2014`,
   `U+2026` - in addition to the existing NFKC and whitespace normalisation. Excerpt
   mismatch errors now include a bounded span of the same source near the divergence,
   located deterministically and capped at 160 characters. No fuzzy matching, edit distance,
   case folding or word reordering was introduced.

All 92 excerpts in the 14 successful live dossiers were re-verified under the widened
normaliser and still match their sources.

### Regression tests added

| Guarantee | Test |
| --- | --- |
| Failed candidate does not retain a stale dossier | `test_a_failed_candidate_does_not_retain_a_stale_dossier` |
| Failed candidate remains in the report | `test_a_failed_candidate_remains_visible_in_the_report` |
| Other candidates untouched by cleanup | `test_cleanup_never_touches_another_candidates_dossier` |
| Cleanup is safe with no previous dossier | `test_cleanup_is_idempotent_when_no_dossier_exists` |
| Successful retry writes its dossier | `test_a_successful_retry_still_writes_its_dossier` |
| Deletion confined to a validated path | `test_delete_evidence_is_confined_to_a_validated_company_path` |
| Curly and straight quotes, dashes and ellipsis match in both directions | `test_typographic_variants_of_the_same_punctuation_match` (9 cases) |
| The exact budibase excerpt now validates | `test_the_exact_budibase_excerpt_now_validates` |
| Paraphrase, case change and reordering still rejected | `test_punctuation_folding_does_not_admit_a_paraphrase` (5 cases) |
| The fold stays a closed set | `test_only_the_seven_documented_characters_are_folded` |
| Exact excerpts still pass | `test_an_exact_ascii_excerpt_still_passes` |
| Mismatch error carries a bounded closest span | `test_a_mismatch_error_shows_the_closest_span_from_the_correct_source`, `test_the_diagnostic_span_is_bounded` |
| Diagnostic cannot leak another source or candidate | `test_the_diagnostic_cannot_quote_another_source_or_candidate` |
| Wrong-source attribution still names the right source | `test_a_wrong_source_attribution_still_names_the_right_source` |
| Unanchored excerpt keeps the generic message | `test_a_wholly_unrelated_excerpt_keeps_the_generic_message` |

Verified against the pre-fix source: 14 of these fail without the change and pass with it.
The remainder are invariance tests that must pass in both states.

The provider was not called during this fix.

## Regenerated live run and final acceptance audit

After both fixes were applied, evidence extraction was re-run against
`anthropic` / `claude-sonnet-5` on the `source-test` run.

### Run totals

- **15 successful dossiers**, no failures.
- 79 claims, 73 unknowns, 1 conflict.
- 2 candidates retried (`agentic-commits`, `ticketdesk-ai`); 17 attempts in total.
- 104,272 input tokens, 24,226 output tokens.

Claim distribution: `product` 46, `traction` 20, `market` 7, `risk` 3, `team` 3.
Verification: `company_claim` 65, `community_signal` 13, `independently_supported` 1.
Inference: `explicit` 78, `inferred` 1.

### Offline acceptance revalidation

The artifacts were audited without calling the provider. Every check was recomputed from
the persisted dossiers and request artifacts rather than read back from the report:

- all **79 deterministic claim IDs** re-derived and matched;
- all **101 source excerpts** re-verified against the specific source they cite, under the
  documented punctuation normalisation;
- **zero invalid source references** - no claim cites a source outside the set supplied for
  its own candidate, and no claim cites a source absent from its own dossier;
- **zero cross-candidate leakage** - no `source_id` appears under more than one candidate;
- **zero secret or local-path exposure** - eleven patterns scanned across every Stage 4
  artifact (Anthropic keys, generic `sk-` keys, bearer tokens, `authorization`, `cookie`,
  `headers`, `x-api-key`, environment dumps, `*_KEY`/`*_TOKEN`/`*_SECRET` names, JWTs,
  absolute filesystem paths), all zero.

`evidence-report.json` reconciles with the dossiers on every figure, and the token totals
reconcile against the 17 persisted response artifacts.

### Budibase recovery

`budibase-agents-beta` succeeded on its **first** attempt and produced 10 claims. The
excerpt that failed twice on the previous run is present and validating:

> `ev-c6a5050dd503` - "Today, we're launching Budibase Agents into Beta, empowering our
> users to build custom agents with their own models, APIs, and data"

The source still writes `we’re` with `U+2019` and the model still emits `U+0027`. The
documented closed punctuation fold reconciled them, which is exactly the difference that
previously rejected a nine-claim dossier.

### Zero-claim dossiers

`gibsonai-com` and `lumro` both produced valid dossiers with zero claims.

- `gibsonai-com`: 5 unknowns and three warnings recording that four of its five supplied
  sources are from `memorilabs.ai`, a different domain from the candidate. Validation
  removed nothing; the empty claim list was the model's deliberate answer to an identity
  conflict.
- `lumro`: 5 unknowns, `website_available: false`, and the structural warning that missing
  evidence is not evidence of weakness. Only one Hacker News source of 384 characters was
  available.

In both cases the absent evidence was **preserved as uncertainty rather than converted into
a negative conclusion**. Neither dossier contains a claim asserting or implying anything
adverse about the company.

### Known cleanup limitation: stale attempt artifacts

Four request and response files from the previous live run remained under `llm/` after the
`--force` re-run:

- `llm/evidence-requests/budibase-agents-beta-attempt2.json`
- `llm/evidence-responses/budibase-agents-beta-attempt2.json`
- `llm/evidence-requests/dooza-desk-attempt2.json`
- `llm/evidence-responses/dooza-desk-attempt2.json`

Both candidates succeeded on attempt 1 in the regenerated run, so no attempt 2 was written
and the earlier files were not cleared. This leaves 19 attempt artifacts on disk against 17
attempts actually made.

This is **non-blocking**:

- `evidence-report.json` is authoritative on attempt counts and outcomes, and it disagrees
  with the stale files correctly;
- Stage 5 reads the evidence dossiers and the report, not `llm/`;
- the stale files are timestamp-distinguishable - they carry the previous run's timestamp
  (`13:50:23Z`) rather than the regenerated run's (`17:50:25Z`);
- `outputs/runs/*` is gitignored, so these files cannot enter a commit;
- the final demonstration will use a fresh run ID, which starts with an empty `llm/`.

It is recorded here as a **known cleanup limitation for future work**: the stale-artifact
cleanup added in this stage covers `evidence/` only. Per-attempt request and response files
are written as attempts occur and are never removed, so a `--force` re-run that needs fewer
attempts than its predecessor leaves the surplus behind.

### Note for Stage 5: independently_supported is not automatic credit

One claim in `agentic-commits` (`ev-08b4f0078642`) carries
`verification_status: independently_supported`. It satisfies the mechanical requirement -
two distinct supplied sources, each cited and each carrying a verified excerpt - but the
two sources support **different parts of a compound statement**: the founder's own blog
index supports the article's existence and title, while the Hacker News excerpt ("The
thread has 2 points on Hacker News.") supports only that a thread exists. It is not the
same fact corroborated by two voices.

Nothing here is fabricated and every part traces to a source. The consequence is for
scoring: **Stage 5 must not award automatic score credit merely because a claim carries
`independently_supported` status.** It must evaluate whether the cited evidence actually
supports the rationale the score rests on. The validator enforces the mechanical rule and
cannot detect a compound claim without semantic reasoning.

## Personal observations

TODO(author): whether the excerpt-verification strictness (whitespace normalisation only)
is the right trade once you have seen how often a live model reformats a quotation, and
whether `normalize_for_match` should be loosened.

TODO(author): your read on the claims produced by the first live run - in particular
whether `independently_supported` is being used at all, given that a company's own pages
never satisfy it.

TODO(author): whether the four-page, 18,000-character input bound gives the model enough to
work with, or starves it.
