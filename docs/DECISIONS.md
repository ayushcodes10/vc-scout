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

---

## D29 - The score measures the evidence-backed case, not the company

**Decision.** Every scored dimension carries an `assessment_status`, and the status caps the
score: `supported` may use the full range, `partially_supported` 70% of the maximum,
`not_assessable` 50%, `contradicted` the full range with an explanation of the contrary
evidence. Ceilings are floored, never rounded up.

**Why.** A model asked to score a company it knows little about will produce a confident
number anyway. Tying the ceiling to the evidence status means a well-written narrative on
thin material cannot earn points the sources do not carry - and, equally, that a gap does
not force a zero. `not_assessable` is deliberately *not* pinned to zero or to the midpoint:
a dimension with one weak company claim and a dimension with nothing at all are different,
and the model has to say which it is and why.

**Cost.** Two companies with the same total are not directly comparable unless you also
read how much was assessable, which is why `scored_out_of` is reported alongside. And a
genuinely excellent company with a thin web presence scores low here. That is the intended
reading - the score is about the case, and the confidence figure says how much was found.

## D30 - Research confidence is computed, and its formula is fixed

**Decision.** Confidence is computed from countable coverage facts after the model answers.
The model is never asked for a confidence figure, and the schema does not contain one.

    source_coverage = cited sources / supplied sources
    claim_volume    = min(claims / 8, 1)
    category_span   = distinct evidence categories / 5
    corroboration   = min(corroborated findings / 3, 1)
    website         = 1 if a website page was read, else 0
    independence    = share of claims that are not company_claim

    score = 0.15*source_coverage + 0.20*claim_volume + 0.20*category_span
          + 0.15*corroboration  + 0.15*website      + 0.15*independence

    penalties = min(0.25 * identity_warnings, 0.25)
              + min(0.05 * conflicts,         0.10)
              + min(0.01 * unknowns,          0.10)

    confidence = clamp(score - penalties, 0.0, 1.0)

Thresholds: **high >= 0.65**, **medium >= 0.40**, **low** below that. A dossier with no
claims scores **0.0** outright.

**Why.** Self-reported confidence tracks fluency, not evidence. Every input here is
something the pipeline observed about its own research. The penalties are bounded so no
single factor can dominate, and unknowns are penalised only gently - recording what you do
not know is honest behaviour and should not be punished like a contradiction.

**Cost.** The weights are a judgment call with no ground truth behind them. They are
versioned, surfaced in every artifact, and documented here so a reader can disagree with
them explicitly rather than discovering them by inference.

## D31 - The independently_supported label earns nothing on its own

**Decision.** Confidence counts only the findings the analysis explicitly records under
`corroborated_findings`, each naming a fact and the claims behind it. The
`independently_supported` verification status on an evidence claim contributes nothing by
itself, to either the score or the confidence.

**Why.** The label is mechanical - two cited sources - and the first live evidence run
produced one where the two sources supported *different halves of a compound statement*
(a blog post's existence, and that a Hacker News thread existed). That is not the same fact
corroborated by two voices. A validator cannot tell the difference without semantic
reasoning, so corroboration has to be asserted about a named fact and can then be read back
and checked by a person.

**Cost.** A genuinely corroborated finding that the model forgets to record earns no
confidence credit. Under-crediting is the safer error here.

## D32 - Guardrails, not thresholds, decide a meeting

**Decision.** The score band is the starting point, and then:

1. `take_a_meeting` additionally requires at least medium confidence, an identifiable
   product, an identifiable buyer, no unresolved identity warning, and evidence reaching at
   least four scoring dimensions.
2. A `pass` band with more than three unassessable dimensions, low confidence and no
   evidenced thesis mismatch becomes `watch` for insufficient evidence.
3. A zero-claim dossier becomes `watch` with an insufficient-evidence rationale.
4. An unresolved cross-domain identity mismatch caps the recommendation at `watch`.
5. A missing website never, on its own, forces a pass.
6. An evidenced thesis mismatch may still produce `pass`, even at low confidence.

Every guardrail that fires is recorded by name in the artifact, alongside the band it moved
from, the model's suggestion and whether the two disagreed.

**Why.** A number alone cannot distinguish "we looked and it is weak" from "we could not
look". Guardrail 2 is the one that matters most: without it, an unreachable website and a
sparse dossier would read as a considered rejection.

**Honest limitation.** Guardrail 1's four-dimension requirement is defence in depth rather
than the active mechanism: with only three evidenced dimensions the status ceilings cap the
achievable total at 74, below the meeting band, so it cannot fire through the normal path.
It is retained because it makes the requirement explicit and would catch a future change to
the ceilings. A test pins that arithmetic rather than asserting it from memory.

**Cost.** Six interacting rules are harder to reason about than one threshold. They are
applied in a fixed order, each is named in the output, and each has its own test.

## D33 - Analysis reads evidence, never raw pages

**Decision.** The analysis stage reads `candidates.json`, the evidence dossiers and the
evidence report. Raw pages, raw Hacker News responses and the web are all unreachable from
it.

**Why.** By this point the pipeline has already decided what counts as evidence, and every
claim has been verified against the page it came from. Letting analysis reach back to raw
text would create a second, unvalidated path to a claim and quietly undo the excerpt
verification the previous stage exists to provide.

**Cost.** Anything the evidence stage failed to extract is invisible to analysis, so an
extraction miss becomes an analysis gap. That is the correct failure direction - it shows up
as `not_assessable` with a recorded unknown rather than as an unsourced assertion.

---

## D34 - DISPROVEN: the enum-with-null hypothesis

**Superseded by D37. This decision recorded a hypothesis that a later controlled live run
disproved. It is kept, marked, rather than deleted, because the reasoning that produced it
is part of the record.**

After the first live Stage 5 run failed with HTTP 400 `invalid_request_error` for all
fifteen candidates, a schema diff found exactly one construct the analysis tool used that
the evidence tool did not: an `enum` containing `null`, at
`model_suggested_recommendation`. That was recorded as the probable cause, explicitly
flagged at the time as "a strong inference from persisted evidence, not a proven cause",
because the provider had discarded the API's own error message.

**It was wrong.** With the message restored (D35), the next controlled run returned:

> The compiled grammar is too large, which would cause performance issues. Simplify your
> tool schemas or reduce the number of strict tools.

The rejection was about the *size* of the compiled grammar, not about any single construct.
The null-bearing enum was a coincidence of the diff, not the cause.

The change that decision made - a plain string enum, left out of `required` - is retained
under D37, because it is smaller and no worse. But it was not the fix, and it did not make
the first live attempt succeed.

**What the episode is worth keeping for:** a diff that leaves exactly one difference is
suggestive, not conclusive, and the honest label at the time ("inference, not proven cause")
was the right one. D35 is what turned the second attempt into a diagnosis instead of another
guess.

## D35 - Provider errors record the API's own message

**Decision.** An HTTP error detail now carries the provider's `error.message` alongside the
type, whitespace-collapsed, truncated to 400 characters and scrubbed of key-shaped text.

**Why.** The previous version recorded only the status and error type, on the reasoning that
a response body can echo request content. That reasoning was wrong in one important way: the
request content is already persisted in the request artifact beside it, so the message
discloses nothing new - while its absence made a deterministic HTTP 400 undiagnosable from
the artifacts, which is exactly the situation the persistence exists for.

**Cost.** Error details are longer. The scrub is defensive only; the API does not echo
credentials.

## D36 - Provider failures are classified, and run-level ones stop the run

**Decision.** `LlmError` carries `run_level`. The Anthropic provider sets it for HTTP 400,
401, 403 and 404, and for a missing credential. When a stage sees a run-level failure it
stops issuing requests; the remaining candidates are recorded as not attempted, with the
reason, and any artifact left from an earlier run is cleared for them too.

Three classes, three behaviours:

| Class | Examples | Behaviour |
| --- | --- | --- |
| Run-level | 400 bad schema, 401/403 credential, 404 unknown model, missing key | Stop the run; record every remaining candidate as not attempted |
| Transient | 408, 409, 429, 5xx, timeouts, connection faults | Retry once within the candidate |
| Candidate-specific | 413 request too large, malformed response, validation failures | Fail that candidate, continue the run |

**Why.** The failed run made fifteen identical doomed requests. Every one was rejected for a
property of the run, not of the company, and none could have succeeded. On a live provider
that is wasted latency and, for some error classes, wasted spend.

**Why not fail fast on everything.** A 413 is about one oversized dossier and says nothing
about the next candidate; a 429 is about timing. Collapsing those into a run-level stop would
lose fourteen good analyses to one unlucky one.

**Cost.** A genuine per-candidate 400 - if the API ever returned one for a single company's
content rather than for the schema - would now stop the run. That is the safer direction:
stopping is recoverable and visible, whereas fifteen identical rejections look like fifteen
separate problems.

---

## D37 - A compact provider schema, separate from the analysis contract

**Established root cause.** The controlled live run returned::

    HTTP 400 invalid_request_error: The compiled grammar is too large, which would cause
    performance issues. Simplify your tool schemas or reduce the number of strict tools.

**Decision.** The provider-facing schema is now a deliberately small artifact whose only job
is to compile into a decoding grammar. The analysis contract is unchanged and lives where it
always did, in `analysis_validation.py` and the models.

Four simplifications, in order of effect:

| Change | Before | After |
| --- | --- | --- |
| One shape for every grounded statement, tagged by `kind` | 6 distinct object definitions | 1 |
| All descriptions removed - they duplicate the versioned prompt | 2,086 chars | 0 |
| `maximum` dropped; the rubric supplies it | model echoed a constant | validator fills it |
| `thesis_assessment` flattened to `thesis_fit` + a `thesis` section | 1 extra object | 0 |

Serialized size fell from **6,034 to 1,829 bytes**, distinct object shapes from **9 to 3** -
below the evidence tool, which the API has compiled successfully in a live run.

**What did not change.** Exactly one forced strict tool. `strict: true` still set,
`tool_choice` still pinned, parallel tool use still disabled. No free-form JSON, no prose
parsing, no splitting a candidate across calls. Every vocabulary is still enum-constrained
in the schema: the seven dimensions, the four assessment statuses, the three
recommendations, the four thesis verdicts and the seven section kinds.

**What moved to local validation** - all of it already enforced there, none of it newly
invented: the seven exact components each appearing once, the configured rubric maxima, the
assessment-status ceilings, the recomputed total, evidence and unknown reference integrity,
grounding of every section and risk, the requirement that a competitor be named only from a
claim, the market-size scrubber, and the two-or-three recommendation changers.

**Cost.** The model now writes a flatter shape than a reader of the persisted analysis would
expect - `sections` with a `kind` rather than named fields, and `text` rather than `fact` on
a corroborated finding. The prompt explains it and the validator maps it back, but there is
one more translation step between what the model emits and what is stored.

**Guarding it.** A budget test measures the serialized schema and fails above 2,400 bytes or
four object shapes, with a message saying to move the rule into the validator rather than
raise the number. Raising it should be a deliberate decision re-verified against the API.

## D38 - Single-candidate runs

**Decision.** `analyze --company-id <id>` analyses one candidate. Every other analysis is
left exactly as it was - not re-run and not deleted - the report records `filtered_to`, and
an unknown ID raises before any provider call.

**Why.** Verifying a schema change against the live API cost fifteen requests the first time
and one the second, only because fail-fast happened to catch it. A filter makes "one paid
request, then look" the normal way to verify, rather than something that depends on the
failure mode being convenient.

**Cost.** A filtered report is a partial record of the run, which is why it is marked. The
overwrite guard is also narrowed to the named candidate, so analysing a fresh candidate is
not blocked by other candidates' existing analyses.

## D39 - A run owns its attempt files

**Decision.** Before a candidate is processed, that candidate's persisted attempt files for
that stage are deleted. After a run, the attempt files on disk are exactly the attempts the
report records - no more, no fewer. Cleanup is per candidate and per stage: it validates the
company ID, stays inside the run directory, and never touches a dossier, an analysis, an
extracted page or a source artifact.

**Why.** A run that needed two attempts left `attempt2` files behind; the next run needed
one, wrote `attempt1`, and the stale pair survived. Disk then recorded failures for
candidates the report says succeeded on the first try. The audit script written to check the
live run read those files and reported a false failure - the defect misled its own reviewer,
which is the strongest argument that "leave old artifacts alone" is the wrong default here.

**Cost.** A run can no longer be used to inspect the previous run's failed attempts for the
same candidate; that history has to come from the previous run's report, or from a separate
run id. Given that the alternative is a directory that silently contradicts the report, that
is the better trade.

## D40 - The report says when the meeting band was out of reach

**Decision.** Each analysis outcome carries `maximum_achievable_score` and
`meeting_reachable_by_statuses`, computed from the recorded assessment statuses and the
rubric ceilings. They are report metadata only: derived after scoring, never inputs to the
score, the confidence or the recommendation.

**Why.** In the live run no component was ever graded `supported`, so the ceilings alone held
every achievable total under 80 - the take-a-meeting band was unreachable for all 15
candidates before any judgement about the companies. A reader seeing fifteen `pass` calls
would reasonably conclude fifteen weak companies. The honest reading is that the evidence was
thin, and the report should say which of the two it is rather than leave it to be
reconstructed from the ceiling table.

**Cost.** Two more fields to keep coherent with the scores, and a headroom number that is
easy to misread as a prediction of what the company could score with better evidence. It is
not: it is the ceiling implied by the statuses actually recorded on this evidence.

## D41 - Rendering is deterministic, not another model call

**Decision.** Memos and the ranking are rendered from validated artifacts by Jinja2
templates that place finished strings. No language model is involved in the memo text, the
one-sentence call, the ranking order or the explanation of why a call was made. The output
carries no generated timestamp, every collection is sorted, and the same artifacts produce
byte-identical files.

**Why.** The pipeline already spends two model calls and one deterministic policy on each
company. A third call to "write the memo" would be a fourth opinion sitting on top of those
three, free to soften a pass, dramatise a risk, or restate a score it had no part in
computing - and nothing downstream would catch it, because prose is not schema-checkable.
Rendering is also the only stage a reviewer can verify by re-running it: if the bytes match,
the memo is the artifacts.

**Cost.** The wording is a template, so it is less fluent than a model would write, and every
new phrasing case - a new guardrail, a new call kind - has to be added in code. That is the
right place for it: those cases are decisions, and decisions belong in reviewed code.

## D42 - The reader cites [S1], the pipeline cites ev-…

**Decision.** A memo's reader-facing citation is a compact marker, `[S1]`, assigned in the
order the document first uses it and resolving to exactly one entry in a numbered source
list at the foot of the memo. Internal identifiers - `ev-…` for evidence claims, `unk-…` for
recorded unknowns - never appear as a reader-facing citation. A statement anchored only to a
recorded unknown is labelled *Open question* rather than left unattributed.

**Why.** Both audiences are real and they need different things. A partner needs to know
which page a sentence came from, in one glance, with a URL to click. A reviewer needs to
follow the chain back through claim IDs to excerpts. Showing the reviewer's identifiers to
the partner makes the memo unreadable; showing the partner's markers to the reviewer loses
nothing, because the analysis artifact keeps the full chain.

**Cost.** One more mapping to keep honest, and marker numbering depends on document order -
reordering a section renumbers every citation in it. Two tests hold the invariants: every
marker resolves to exactly one entry, and every listed source is cited above.

## D43 - Untrusted text may contribute words, never structure

**Decision.** Every string in a memo that did not originate in this codebase - a company
name, a page title, an excerpt, the model narrative written from those pages - is
neutralised before rendering: whitespace collapses so it cannot span lines, control and
bidirectional characters are dropped, and Markdown's inline structural characters are
escaped. Source URLs render as autolinks, so link text always equals link target. Only
`http` and `https` become links at all.

**Why.** A memo is assembled from pages the firm does not control. Pasting that text into
Markdown hands whoever wrote the page the ability to add a heading, forge a scorecard row,
embed a tracking pixel, or show "Verified filing" over a link to somewhere else. The last of
those is why link text is never separate from the URL: a deceptive label is simply not
expressible in this renderer.

**Cost.** Escaping is visible in the raw Markdown - `not\_assessable` rather than
`not_assessable` - and legitimate emphasis in source text is flattened. Rendered output is
unaffected, and the alternative is trusting a stranger's website with the memo's structure.

## D44 - The ranking is a triage queue and says so

**Decision.** `ranking.md` sorts by recommendation, then score descending, then confidence,
then company name, and states in the document that this is the order to work the list in
rather than a quality ordering. When no candidate reaches the meeting band, a section
explains why from the run's own counts - assessment statuses across every scored slot, how
many candidates the ceilings put out of reach - and no score is raised to fill the band.

**Why.** Watch outranks pass in a triage queue because a watch needs an action. But a watch
that exists only because the research came up short says nothing good about the company, and
a pass on a company the evidence positively places outside the thesis is a *finding*. An
ordering cannot express that difference, so leaving it implied would invite exactly the
misreading this pipeline is built to prevent.

**Cost.** A reader who only scans the table still sees watch above pass. The prose above it,
the per-row primary rationale, and the memos themselves all carry the distinction.

## D45 - The site is generated HTML with no build step and no dependency

**Decision.** The static site is Jinja2 templates plus one hand-written stylesheet and one
small vanilla script, copied verbatim beside the generated pages. No bundler, no framework,
no CDN, no web font, no remote image. Every page declares
`default-src 'none'` with `style-src`/`script-src`/`img-src`/`font-src` limited to `'self'`.

**Why.** The deliverable is a directory a partner can open with
`python3 -m http.server` and read offline, and that a reviewer can diff. A build step makes
the artifact depend on a toolchain that has to be installed and pinned; a CDN makes it
depend on a network and on somebody else's uptime. Both trade away the property that
matters most here - that the site is a rendering of committed artifacts and nothing else.

**Cost.** The CSS and JS are maintained by hand, and there is no component model to reuse
across the two templates. At this size that is cheaper than the alternative; a third page
type would be the point to reconsider.

## D46 - Escaping runs the opposite way from the memo renderer

**Decision.** The site's Jinja environment has autoescape **on** and its view models carry
raw text, which is the mirror image of the Markdown renderer, where escaping is applied in
Python before a value reaches a template. Exactly one value in the site is marked safe: the
JSON block the filters read, escaped by a dedicated function that turns `<`, `>`, `&`,
U+2028 and U+2029 into unicode escapes.

**Why.** HTML has a correct, context-aware escaper built into Jinja, and Markdown does not.
Using each format's own mechanism is safer than inventing a second one. The JSON block is
the exception because HTML escaping there would be *wrong*, not merely unnecessary: a
browser does not decode entities inside a `<script>` element, so an autoescaped `&quot;`
would corrupt the data rather than protect it. What is marked safe is a serialiser's output
under a stricter escape, not an untrusted string.

**Cost.** Two renderers now hold two opposite escaping rules, which is a real thing to
remember. Both are stated at the top of their modules, and the shared view-independent
pieces - the call wording, the guardrail labels, the source index, the ranking comparator -
are imported rather than duplicated, so the parts that could silently disagree do not.

## D47 - JavaScript may reorder the page; it may never write it

**Decision.** Every row is server-rendered. The script toggles the `hidden` attribute,
reorders existing nodes, and writes exactly one string - the result count - through
`textContent`. No markup-assigning property appears in the file, and there is no `fetch`,
no `XMLHttpRequest` and no dynamic import. The page is complete and correct with JavaScript
disabled; the filter form is hidden until the script enables it.

**Why.** Any renderer that builds markup from data has to escape correctly on every path,
forever, and one missed path is an injection. Not building markup at all removes the class
of bug rather than defending against it. It also means the filters degrade to a full,
correct table instead of an empty one.

**Cost.** Filtering is limited to what can be expressed by hiding and reordering rows that
already exist - no server-side paging, no infinite scroll, no result highlighting. For
fifteen candidates, and for a static artifact, that is the whole requirement.
