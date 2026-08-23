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

---

## D9 - Query words are sent as optional, not required

**Decision.** Three of the four query variants send Algolia's `optionalWords` parameter
containing the full query. One variant keeps the default strict behaviour.

**Why.** Algolia requires every query word by default. `"AI agents for SMB operations"`
against `tags=show_hn` returns **1 hit** - the first live run of this stage produced a
single candidate against a requested fifteen. Sending the words as optional turns the
search into a relevance-ranked OR: hits matching more of the query still rank first, but
the long tail becomes reachable. The same run then returned 271 hits and a full set of
fifteen candidates.

**Cost.** Recall is bought with precision. The broad variants drag in popular launches
that match only one or two query words, which is what the relevance component of the
discovery rank (D10) exists to push back down.

## D10 - Discovery rank includes topical relevance, and it is weak here

**Decision.** Discovery rank is
`0.35 * relevance + 0.35 * engagement + 0.15 * recency + 0.15 * variant`, where relevance
is the share of the query's content words found in the story title or URL.

**Why.** Without a topical term the rank is dominated by engagement, and the top of the
list becomes "whatever was on the front page that month" rather than "what matches this
query". Adding relevance demoted several popular but unrelated launches.

**Honest limitation.** Measured over one live run of the demo query, relevance took the
value 0.50 for 88 of 109 accepted hits. `ai` and `agents` match almost every story in this
corpus, while `smb` and `operations` match almost none, so the component separates the
tails (it removed 8 zero-relevance hits from contention) but is close to constant in the
middle. It is a genuine improvement, not a strong signal, and no synonym table was added
to prop it up - guessing that "SMB" means "small business" is exactly the kind of
inference that belongs in the analysis stage with evidence behind it, not in a ranking
heuristic.

**Cost.** A fourth weight to justify, and a component that looks more discriminating in
the formula than it is in practice on this query.

## D11 - A top-level domain is not a relevance match

**Decision.** The final label of a hostname is stripped before relevance matching.

**Why.** Found while reading a live run: the query term `ai` scored a full relevance hit
against every `.ai` domain, which is a fact about domain fashion rather than about the
company. Dropping the TLD removes it.

**Cost.** A term that genuinely only appears in a TLD is now invisible. That is the
correct trade.

## D12 - Incumbents are rejected as their own class

**Decision.** `INCUMBENT_DOMAINS` is separate from `BLOCKED_DOMAINS`, and produces the
distinct rejection reason `incumbent_domain`.

**Why.** Blocked domains are *not a company's product surface* (code hosts, publishers,
discussion boards). Incumbents are a different objection: `deepmind.google` is a perfectly
good product page, but a seed fund cannot invest in it. Collapsing the two would hide the
difference in the sourcing report, and these are the entries most likely to need revisiting.

**Cost.** A hand-maintained list that will drift. It is small, explicit, and counted in
the report so its effect is visible.

## D13 - Discovery rank never reaches the investment score

**Decision.** `DiscoveryRank` is computed from Algolia response metadata alone - points,
comments, age, which variant found the story, and word overlap with the query. It orders
candidates for enrichment and is never read by `vc_scout.policy`.

**Why.** Discovery rank is a budget decision: which companies are worth fetching pages
for. Letting it leak into scoring would mean an HN front-page hit scored better on the
thesis than a quiet company with better evidence.

**Cost.** The two numbers can disagree visibly - a top-ranked candidate may score poorly
and be recommended *pass*. That is correct behaviour and the memo should not apologise for it.

---

## D14 - Ranking is lexicographic, led by relevance class

**Decision.** Discovery ordering is `(relevance class, relevance score, quality score)`,
compared in that order. There is no single composite discovery score. `quality_score`
(engagement, recency, variant weight) only breaks ties between candidates that are already
equally relevant.

**Why.** The first formula was a weighted sum in which relevance carried 35%. An audit of
the first live run showed why that failed: 12 of 15 shortlisted candidates scored exactly
0.50 on relevance, because `ai` and `agents` matched nearly every story in the corpus while
`smb` and `operations` matched almost none. A component with three distinct values across
the shortlist cannot compete with engagement, which had fourteen. The result was that a
blog post about a local-model runner ranked first and the only candidate whose title
actually said "SMB" ranked eleventh.

Re-weighting alone would not have fixed it - shifting weight onto a component with three
values only makes the three tiers coarser. Making class the primary key is what removes
the failure mode entirely: no engagement gap, however large, can cross a class boundary.

**Cost.** Two candidates in different classes are not comparable on a single number, so
the UI has to show the class. Within a class the ordering is still a weighted sum with all
the usual arbitrariness.

## D15 - Relevance is concept-group classification, not token overlap

**Decision.** Relevance is computed from three concept groups - AI automation, business
buyer, operational workflow - scored `0.40 * A + 0.25 * B + 0.35 * C` and classified as
`direct` (A and (B or C)), `adjacent` (A only) or `irrelevant` (no A).

**Why.** Flat overlap with the operator's query words measures whether a founder used the
same vocabulary as the partner, which is not the same question as whether the product fits.
Grouping the vocabulary means a story can qualify through *any* recognised workflow term,
so "AI agent for customer support tickets" is direct without ever saying SMB.

Requiring group A is what stops the obvious inversion: a bookkeeping tool for small
businesses with no AI in it matches B and C strongly and is still `irrelevant`, because
groups B and C qualify an AI product rather than substituting for one.

**Cost.** Three hand-maintained vocabularies that will drift, and a real false-positive
surface - `lead`/`leads` and `team`/`teams` are generic SaaS words. `store` was already
removed after it classified "embeddings store for AI agents" as a retail product. Every
matched term is persisted on the candidate so these are visible rather than silent.

## D16 - Query variants encode the thesis, not just the query

**Decision.** Nine of the twelve variants pair the query's own AI-automation wording with a
fixed facet - SMB, small business, business operations, customer support, sales, finance,
scheduling, back office, ecommerce.

**Why.** The relaxed single query returned a pool dominated by whatever "AI agents" was
popular that month. Searching each workflow explicitly reaches products that describe a
specific job without using the operator's phrasing. The facets are fixed because they
describe the *fund's* thesis surface; the AI wording is taken from the query so a partner
searching for "automation" is not silently switched to someone else's vocabulary.

**Cost.** Twelve requests per run instead of four, and a facet list that encodes one firm's
thesis. Every variant and its hit count is listed in the sourcing report.

## D17 - Shortlists are short rather than padded

**Decision.** Irrelevant candidates are removed before ranking and truncation. The
shortlist fills from direct candidates first; adjacent ones are held to 30% while direct
supply lasts. If fewer defensible candidates exist than were requested, the run returns
what it found and reports `shortfall`.

**Why.** A partner reading a list of fifteen assumes fifteen were worth reading. Padding to
the requested limit with off-topic results converts a discovery problem into a wasted
analysis budget and a misleading artifact.

Filtering happens before truncation specifically so that a popular but irrelevant story
cannot occupy a slot and then be removed, leaving the shortlist shorter than it needed to be.

**Cost.** A run can return fewer candidates than the assignment's 10-20 target. That is a
reporting outcome, not a failure, and the report says which part of the funnel was thin.

## D18 - The launch URL is a source; the website is an origin

**Decision.** Each candidate carries two sources - the Hacker News thread and the launch URL
exactly as posted. `Candidate.website` is the launch URL reduced to its origin when it
carries a path or query, with a note recording the change.

**Why.** A Show HN link often points at a blog post or a deep product route. Enrichment
wants the company's front door; citation wants the exact page the claim came from. Keeping
both means neither is lost.

**Cost.** Two sources per candidate instead of one. The host is never rewritten - only the
path is dropped - so this can never turn a link into a different company's website, but it
also means subdomains such as `app.example.com` are kept rather than folded to the apex.
