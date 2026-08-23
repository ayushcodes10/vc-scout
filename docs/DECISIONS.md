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

---

## D19 - BeautifulSoup with an explicit heuristic, not Trafilatura

**Decision.** Extraction uses BeautifulSoup with a hand-written main-content heuristic:
remove chrome and noise, prefer `<main>`/`<article>`/`[role=main]`/`<body>` in that order,
then keep block-level text in document order.

**Why.** The plan named Trafilatura. Against it: this stage also needs link discovery,
heading extraction and title recovery from the same parse, and its behaviour has to be
pinned by golden-style tests. A heuristic written here is auditable line by line and
deterministic across versions; Trafilatura's output can shift with a release and would need
a second parser alongside it anyway.

**Cost.** Trafilatura is genuinely better at extracting article bodies from
content-heavy pages. On a startup marketing site - which is what this stage almost always
reads - the gap is small, but a blog post is where this loses.

## D20 - Enrichment never removes a candidate

**Decision.** Every candidate gets a `PageBundle` written, including candidates whose site
returned nothing at all. An empty bundle carries `status=failed`, the categorised failures,
and a warning stating that evidence is missing rather than negative.

**Why.** The recommendation policy already separates "we found nothing" from "we found
something bad". That separation is worthless if the pipeline silently drops the companies it
could not read - the shortlist would quietly become "companies with good websites", and a
partner would never see the omission.

**Cost.** Downstream stages must handle empty bundles everywhere rather than assuming text
exists, and some analysis budget is spent on companies with nothing to analyse.

## D21 - Fetch safety is enforced in one place, with DNS resolved first

**Decision.** `SafeFetcher` is the only module that makes outbound requests. It resolves the
hostname *before* connecting and refuses any result in loopback, private, link-local,
multicast, reserved or unspecified space; it follows redirects manually so every hop is
revalidated; it abandons a response mid-stream at the byte ceiling rather than downloading
then measuring.

**Why.** Every URL reaching this stage came from third-party text, including URLs found on
pages that other third-party text pointed at. Checking the string is not enough: a public
hostname can resolve to `169.254.169.254`, and a safe first URL can redirect to an unsafe
second one. Resolving first and revalidating each hop closes both.

**Cost.** A DNS lookup on the client side in addition to the one the connection makes, and
a small TOCTOU window between the check and the connection that this design does not close.
Closing it properly would mean pinning the connection to the validated address, which httpx
does not expose cleanly.

## D22 - Four pages per company, chosen by role

**Decision.** The homepage plus at most three additional pages, at most one page per role,
with roles ranked product, pricing, customers, about, team, changelog, blog. The HN launch
URL takes one of the three additional slots when it differs from the site origin.

**Why.** A bounded, role-diverse read produces better evidence per request than depth: one
pricing page and one customers page say more about an SMB workflow product than three blog
posts. Ranking rather than crawling also keeps the stage deterministic and its load on a
small company's site trivial.

**Cost.** A company that explains itself across many pages is under-read, and a site whose
navigation does not use conventional paths yields only its homepage.

---

## D23 - Forced tool use, not free-form JSON

**Decision.** Structured output is obtained by declaring one tool carrying the required
JSON schema, pinning `tool_choice` to it, disabling parallel tool use and setting
`strict: true`. The model's only legal move is to emit one `tool_use` block whose `input`
is the schema.

**Why.** Asking for JSON in prose and parsing the reply invites prose around the JSON,
markdown fences, and silent shape drift. A forced tool moves schema conformance to the API,
which validates before the response is returned, and makes a refusal or a truncation
visible as a *missing tool block with a stop reason* rather than as a confusing parse error.

**Cost.** A schema-shaped constraint the API must compile - a one-off latency cost on first
use - and a schema dialect narrower than JSON Schema: strict mode rejects numeric and
length constraints and requires `additionalProperties: false` everywhere. Excerpt length
bounds therefore live in the validator rather than in the schema.

## D24 - Determinism comes from effort and fixed inputs, not temperature

**Decision.** No sampling parameter is sent. Run-to-run stability rests on a versioned
prompt file, a deterministic source ordering, bounded per-page and per-candidate input, and
a fixed `effort` level.

**Why.** `temperature`, `top_p` and `top_k` are rejected outright by the current Claude
models - sending them returns a 400 - so "temperature 0 for determinism" is no longer
available. What remains controllable is what goes *in*: the same prompt, the same sources,
in the same order.

**Cost.** Identical inputs can still yield different wording. That is why nothing the model
writes is trusted on its own: claim identity is derived from content, and every excerpt is
verified against its source, so run-to-run variation shows up as different claims rather
than as different facts.

## D25 - Excerpts are verified against the source, with whitespace and a closed punctuation fold

**Decision.** Every excerpt must appear in the text of the specific source it is attached
to. Exactly three transformations are applied to both sides before matching, and no others:

1. Unicode NFKC normalisation - so non-breaking, narrow and other typographic whitespace
   behaves like an ordinary space;
2. a **closed** punctuation fold of exactly seven characters:

   | Character | Name | Folds to |
   | --- | --- | --- |
   | `U+2018` | LEFT SINGLE QUOTATION MARK | `'` |
   | `U+2019` | RIGHT SINGLE QUOTATION MARK | `'` |
   | `U+201C` | LEFT DOUBLE QUOTATION MARK | `"` |
   | `U+201D` | RIGHT DOUBLE QUOTATION MARK | `"` |
   | `U+2013` | EN DASH | `-` |
   | `U+2014` | EM DASH | `-` |
   | `U+2026` | HORIZONTAL ELLIPSIS | `...` |

3. whitespace collapse.

**Why the check exists.** This is what makes a fabricated quotation impossible to persist.
An excerpt is a quotation, so it must be the page's own words.

**Why the fold does not permit paraphrasing.** The seven characters above are *rendering
variants of the same punctuation mark*, not different words. Every letter, digit, word and
word order must still match exactly; case is untouched; no other character is mapped. A
paraphrase (`we're` -> `we are`), a case change (`Beta` -> `beta`), a reordering, or a
substituted word all still fail, and each is pinned by a test. Punctuation outside the
table - a guillemet, for instance - is left alone, so the fold cannot quietly grow.

**Why it was widened.** The original rule allowed whitespace normalisation only. On the
first live run that rejected a fully supported nine-claim dossier for
`budibase-agents-beta`, because the source wrote `we’re` (`U+2019`) and the model quoted
`we're` (`U+0027`). One character, everything else verbatim. 13 of the 15 candidates had
the same exposure and avoided it by luck. `U+2026` is in the table for completeness even
though NFKC already decomposes it, so the closed set is legible in one place.

**Cost.** The fold is a real, if small, loosening: a page that deliberately distinguished
an apostrophe from a single quotation mark would no longer have that distinction preserved
in an excerpt. That is an acceptable trade for a check whose purpose is to prove the words
came from the page, not to reproduce its typography.

## D25a - Excerpt mismatches report a bounded diagnostic span

**Decision.** When an excerpt does not match, the validation error includes a short span of
the *same* source near where the excerpt begins to diverge - located by binary search for
the longest matching leading run, bounded to 160 characters, and only when a run of at
least 16 characters anchors it. Otherwise the generic message stands.

**Why.** The retry on `budibase-agents-beta` failed identically twice because the error
said only "copy the excerpt verbatim" and never said what differed. A one-character
apostrophe mismatch is invisible at a glance, so the model re-emitted the same text. The
span makes the difference legible.

**Safety.** The span is drawn only from the source the excerpt was attached to, so it
cannot quote another source or another candidate; it is bounded; and it is diagnostic only
- it never widens what validation accepts. No fuzzy matching, edit distance or paraphrase
acceptance was introduced.

**Cost.** Validation errors are longer, which slightly increases retry input.

## D25b - A failed candidate never keeps an earlier dossier

**Decision.** When evidence extraction finishes without a valid dossier, any pre-existing
dossier for that candidate is deleted before the report is written, via a single validated
company-specific path.

**Why.** Found in the first live run: `evidence/budibase-agents-beta.json` was a leftover
from an earlier `--provider fake` run and survived the live `--force` run in which that
candidate permanently failed. `write_evidence` only runs on success, and `--force` did not
clear the directory, so a downstream stage reading `evidence/` would have seen a failed
company as successfully extracted with zero claims - exactly the silent failure this
pipeline is built to prevent.

**Scope.** The narrowest possible deletion: one validated `company_id`, one path inside
this run's `evidence/` directory. Other candidates' dossiers are never touched, a missing
file is not an error, and a successful retry writes its dossier normally.

**Cost.** A failed re-run destroys a previously good dossier for that candidate. That is
the intended behaviour - a stale dossier is worse than none, because it is indistinguishable
from a current one.

## D26 - Claim identifiers are derived, never model-supplied

**Decision.** The output schema does not ask for a claim ID. After validation, each claim's
ID is computed as `ev-<sha256(company_id, normalised claim, sorted source_ids)[:12]>`.

**Why.** An identifier supplied by the witness is an identifier the witness can reuse,
collide or fabricate. Deriving it makes identity a function of content: the same claim from
the same sources always has the same ID, two identical claims collide and are rejected as
duplicates, and no claim can be given an identity it did not earn.

**Cost.** A claim's ID changes if its wording changes, so IDs are stable across reruns only
while the model phrases the claim the same way.

## D27 - One retry, carrying the validation errors

**Decision.** Invalid output earns exactly one retry. The retry sends the same bounded
source material plus the full list of validation errors. A second failure writes a
structured failure record; the candidate stays in the run with no dossier.

**Why.** Most rejections are mechanical - a mis-attributed excerpt, an over-claimed
`independently_supported` - and are fixed when the model is told precisely what was wrong.
A second failure is a signal about the material, not a reason to keep paying. All errors
are collected before raising so the retry sees the whole list rather than one at a time.

**Cost.** A genuinely borderline company can produce no evidence at all. That is recorded
as a failure with its category, not as a negative finding about the company.

## D28 - Source content is data, in a separate channel

**Decision.** System instructions come from the versioned prompt file and contain nothing
about any company. Source text is passed only in the user message, fenced in explicit
`BEGIN/END UNTRUSTED SOURCE <id>` markers, and introduced as untrusted third-party content.

**Why.** Prompt injection is not hypothetical for this pipeline: the model reads arbitrary
web pages that any founder can edit. Two defences, and the second is the one that matters.
The prompt tells the model that source text is data and that an instruction inside it is
page content. And validation makes compliance irrelevant: a model that obeys an injected
instruction to invent revenue still has to produce an excerpt, and there is no excerpt, so
nothing is written.

**Cost.** Prompt-level defence is advisory and cannot be proven. What is provable, and
tested, is that an invented claim cannot reach an artifact.
