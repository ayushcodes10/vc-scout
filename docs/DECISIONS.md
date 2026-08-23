# Design decisions

Each entry records what was decided, why, and what it costs. Decisions are appended as
stages land; nothing here is retrospective invention.

TODO(author): add your own commentary on any decision you would have made differently.

---

## D1 - Identifiers are content-derived, not sequential

**Decision.** `source_id` is `src-<sha256(normalised_url)[:12]>` and `evidence_id` is
`ev-<sha256(company_id, normalised_claim, sorted(source_ids))[:12]>`.

**Why.** The plan originally specified sequential IDs (`src-001`). Sequential IDs are
readable but unstable: rediscovering the same page in a different order renames it, which
makes citations incomparable across runs and breaks the replay guarantee. Content-derived
IDs are stable by construction, and `EvidenceClaim` validates that an ID actually hashes
to its own content - a fabricated or tampered citation fails to deserialise.

**Cost.** Less readable in raw JSON. Truncating to 12 hex characters accepts a collision
risk that is negligible at the scale of one run (tens of sources).

## D2 - Unknown is a first-class score status

**Decision.** `ScoreComponent.status` is `scored` or `unknown`. An `unknown` component
carries `points=None`, contributes zero to the total, and is reported alongside
`scored_out_of`, so a memo can say "scored on 45 of 100 available points".

**Why.** The guardrail is that missing information must be *unknown*, not automatically
negative. But the rubric still has to produce a 0-100 total. Storing `None` distinct from
`0` keeps the arithmetic honest while making the absence visible everywhere it matters,
and the confidence penalty plus the watch cap stop thin research from hardening into a
confident *pass*.

**Cost.** Two companies with the same total are not directly comparable unless you also
read `scored_out_of`. The ranking view must show both.

## D3 - Confidence is computed, never modelled

**Decision.** `ResearchConfidence` is produced by `vc_scout.policy.compute_confidence`
from four measurable inputs: dimension coverage, source count, whether the company's own
site could be read, and the age of the newest source. A model's self-reported confidence
is not consulted.

**Why.** Self-reported confidence tracks fluency, not evidence. Coverage is something the
pipeline can actually observe about its own research.

**Cost.** The formula's weights are a judgment call and are not calibrated against any
ground truth. They are versioned and shown on the methodology page so a reader can
disagree with them explicitly.

## D4 - The model may suggest a recommendation; the policy makes it

**Decision.** `StartupAnalysis.suggested_recommendation` stores what the model would have
recommended. `RecommendationResult.decision` is produced solely by `policy.decide` from
the total score and confidence. The suggestion is copied into the result as
`model_suggested` for auditing and is never read as an input.

**Why.** This is a change from the original plan, which rejected any model-supplied
recommendation outright. Storing the suggestion separately is strictly more useful: the
guarantee is identical - `test_decision_is_identical_for_every_possible_model_suggestion`
holds the policy blind to it - and it additionally makes model/policy disagreement
measurable, which is the more interesting evaluation signal.

**Cost.** One more field that a reader could mistake for the real recommendation. The
field name, the memo layout and the methodology page all have to keep the distinction
obvious.

## D5 - Recommendation lives beside the analysis, not in its own directory

**Decision.** `analyses/<company_id>.json` holds two top-level keys, `analysis` and
`recommendation`.

**Why.** The required artifact layout has no `policy/` directory, and inventing one would
depart from the specified contract. Separate top-level keys keep the stage boundary
visible, and the recommendation key is simply absent until the policy stage has run.

**Cost.** The file is written twice during a full run.

## D6 - Models forbid unknown keys and are frozen

**Decision.** Every model sets `extra="forbid"`; artifact records are `frozen=True`. The
run manifest is the documented exception, as it accumulates during a run.

**Why.** An unexpected key in an LLM response or a hand-edited artifact is a contract
violation. Silently dropping it is how a `recommendation` field smuggled into an analysis
would go unnoticed - so it raises instead.

**Cost.** Schema evolution requires an explicit `schema_version` bump rather than
tolerant reads.

## D7 - All run paths are constructed in one place

**Decision.** `RunStore` builds every path under `outputs/runs/<run-id>/`, validates
identifiers against strict patterns, and asserts each resolved path stays inside the run
directory.

**Why.** Company IDs derive from third-party text. Path construction scattered across
seven stages is how a hostile company name eventually escapes the output directory.

**Cost.** Stages cannot open ad-hoc files; anything new needs an accessor.

## D8 - JSON is written atomically and deterministically

**Decision.** Writes go to a temporary file in the destination directory, are fsynced,
then atomically replaced. Serialisation uses sorted keys, two-space indent and a trailing
newline.

**Why.** A crashed run must not leave a half-written artifact that later stages read as
valid. Sorted keys keep the committed demo run's diffs reviewable and make byte-identical
replay checkable.

**Cost.** Slightly slower writes; irrelevant at this scale.
