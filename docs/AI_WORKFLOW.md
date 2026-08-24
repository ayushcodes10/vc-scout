# How this was built with AI

An index into evidence that already exists in this repository. Nothing here is a summary
written after the fact: every claim below points at a worklog entry, a decision record, a
prompt file or an artifact you can open.

The short version: the coding agent wrote the code. The author set the goals, the
constraints and the boundaries, reviewed each stage, and made every judgement call that
shaped what the system is allowed to conclude. Nine stages, each ending in a review the
author had to pass before the next began.

---

## Who decided what

| Decided by the author | Implemented by the coding agent |
| --- | --- |
| The whole premise: auditability over fluency - a memo is only worth reading if every sentence traces to a URL | The citation chain that makes it true: excerpt verification, evidence IDs, source markers |
| That the LLM may never make the recommendation | `policy.py`, deterministic and model-free |
| Stage boundaries, and that each stage ends in a manual review before the next starts | Each stage's implementation, tests and worklog entry |
| Repository rules: no commits by the agent, no fabricated history, no AI author trailers | Followed throughout; every commit is the author's |
| The relevance-first shortlist, after seeing engagement dominate the first live run | The relevance model, the eligibility gate and the discovery ranking |
| That company-authored evidence must be able to support a concrete product fact | `assessment_policy.py`, the versioned table and its two mechanical checks |
| That a conflict blocks the dimension it is about, not every dimension near it | `disputed_claim_ids()` and the claim/category relevance rule |
| That a failed candidate is repaired in place rather than by re-running fifteen | `recover-analysis`, the full-report merge and the fingerprint update |
| Which findings were blocking and which were acceptable, at every stage review | The fixes, and the record of what was tried |

The author also rejected work. The enum-with-null hypothesis for the Anthropic 400 was
recorded as a diagnosis and then explicitly marked disproven when the API's own error
message contradicted it (`docs/DECISIONS.md`, D34) - the record keeps the wrong answer
rather than quietly replacing it.

---

## The pipeline, stage by stage

| Stage | What it does | LLM | Worklog |
| --- | --- | :---: | --- |
| `source` | Twelve deterministic Hacker News queries, relevance gate, domain dedup, ranked shortlist | no | [02](../worklog/02-sourcing.md) |
| `enrich` | Bounded fetch of public company pages, SSRF-guarded, robots-respecting | no | [03](../worklog/03-enrichment.md) |
| `evidence` | Claims with verbatim excerpts, recorded unknowns and conflicts | **yes** | [04](../worklog/04-evidence.md) |
| `analysis` | Seven rubric dimensions, narrative, thesis fit, advisory suggestion | **yes** | [05](../worklog/05-analysis.md) |
| policy | Score recomputed, confidence computed, recommendation decided | no | [05](../worklog/05-analysis.md) |
| `recommend` | Markdown memos and the ranking | no | [06](../worklog/06-recommendation.md) |
| `build-ui` | The static site | no | [07](../worklog/07-ui.md) |
| `run` | One command, with resume and fingerprints | no | [09](../worklog/09-orchestration.md) |

**The model is used in exactly two places.** Everything before them is deterministic
retrieval; everything after them is deterministic computation and layout.

Prompts are files, not string literals: `src/vc_scout/prompts/`. Each is versioned
(`evidence_v1`, `analysis_v1` → `v2` → `v2.1`), content-hashed, and the version and hash
are recorded in every artifact produced under it. A prompt is never edited in place - a
behaviour change gets a new version, and the old file stays for comparison.

The worklogs are chronological. Each was written at its stage boundary from what had just
happened, including the failures, and they are not a reconstruction assembled at the end.
`docs/DECISIONS.md` holds 56 numbered decisions with their costs stated.

---

## Representative failures, and what they changed

Each of these was found by running the thing, not by reasoning about it.

| What broke | What it turned out to be | Fix |
| --- | --- | --- |
| The first shortlist was dominated by popular posts | Engagement was outweighing topical relevance, so a viral irrelevant story beat a quiet on-thesis one | Relevance became a gate, not a weight; ranking is relevance-first ([02](../worklog/02-sourcing.md), D11) |
| Internal links resolved against the wrong page | Link resolution used the *requested* URL, not the URL the fetch landed on after redirects | `_fetch_and_extract` returns the final URL; resolution and same-origin checks anchor to it ([03](../worklog/03-enrichment.md)) |
| Budibase's evidence was rejected as fabricated | The page said `we're` with a typographic apostrophe; the model returned a straight one | A closed seven-character punctuation fold before excerpt verification ([04](../worklog/04-evidence.md), D23) |
| A failed run left a previous run's dossier standing | Nothing removed artifacts a later run did not replace | Per-candidate cleanup of dossiers, analyses and attempt files, before each candidate runs ([05](../worklog/05-analysis.md), D39) |
| Every Stage 5 request failed with HTTP 400 | *"The compiled grammar is too large"* - schema size and distinct object shapes, not the enum shape first suspected | A compact provider schema, with the full contract enforced locally ([05](../worklog/05-analysis.md), D34, D37) |
| Zero of 105 component slots were ever `supported` | The prompt read "company-authored" as "cannot support anything", so the meeting band was arithmetically unreachable for all fifteen | Assessment strength separated from source provenance; a versioned policy table ([08](../worklog/08-calibration.md), D48) |
| Two of fifteen analyses failed on shape | Re-running all fifteen would spend 26 requests to fix two | `recover-analysis`: retry the failures, merge into the full report, rebuild downstream (D53) |
| Ticketdesk's product wedge was rejected twice | A traction conflict blocked it because the disputed customer count sat on the same page as the product description | Conflicts block by claim and category relevance, not by shared source (D54) |

---

## How the model's output is constrained

Five mechanisms, in the order they apply:

1. **Strict structured tools.** One forced tool call per request, `strict: true`, parallel
   tool use disabled. No free-form JSON, no prose parsing. Sampling parameters are not sent -
   determinism comes from a versioned prompt, deterministic input ordering and bounded input.
2. **Excerpt verification.** Every evidence claim must carry a short span copied verbatim
   from a supplied source, and that span is checked against the source text before the
   dossier is written. A fabricated quotation cannot survive into an artifact.
3. **Evidence-ID integrity.** The analysis may cite only claim IDs and unknown references
   that exist in *that* candidate's dossier. Every citation is resolved before anything is
   written, and again when a memo or a page is rendered.
4. **Local validation, and one retry.** The compact provider schema exists only to be
   compiled into a decoding grammar; the real contract is `llm/analysis_validation.py` -
   seven exact dimensions, configured maxima, status ceilings, recomputed totals, grounding,
   the market-size scrubber, the self-reported-result cap and conflict relevance. Invalid
   output earns exactly one retry carrying the errors back. A second failure is recorded as
   a candidate failure and the run continues.
5. **Deterministic scoring, confidence and recommendation.** The total is recomputed in
   Python from the model's own per-dimension components. Research confidence is computed
   from coverage facts. The recommendation is made by `policy.py` from the score, the
   confidence and a set of guardrails. The model's suggestion is recorded beside the binding
   call and never consulted - in the live run the two disagreed on 7 of the 15, and the
   policy won every time.

Untrusted input is treated as untrusted throughout: page text is fenced and framed as data,
never instructions; memos escape it so it can contribute words but never structure; the site
autoescapes it and validates every URL before it becomes a link.

---

## One AI call, end to end

`demo/ai-trace/` holds a complete round trip from the live run: the exact request sent, the
raw response, and what validation decided about it.

The candidate is chosen by rule rather than by hand - among successful analyses with at
least three evidence claims, the largest evidence base wins, then the highest score, then
the company ID. `demo/ai-trace/README.md` states the rule and the numbers that satisfied it.

---

## Limitations and open judgements

- **The evidence is thin, and the run says so.** Across 15 candidates the research produced
  79 claims: 65 company-authored, 14 community signal, and exactly **one** about a team.
  The highest total was 42/100, no candidate reached the take-a-meeting band, and the
  ranking explains why from its own counts rather than promoting anyone to fill it.
- **Prompt injection is mitigated, not solved.** Schema-locked output, citation validation
  and escaping remove the leverage; a sufficiently clever page could still influence
  narrative wording. It is documented rather than claimed to be closed.
- **The rubric is uncalibrated.** There is no ground truth here, and no attempt is made to
  claim the weights predict outcomes.
- **`analysis_v2.1` is barely exercised.** It shaped two of fifteen analyses, both through
  the recovery path. Its effect on a full run is unmeasured.
- **The demo is one query, one day.** Sourcing depends on what Hacker News happened to
  contain; a different week is a different shortlist.

---

## Author reflections to complete

TODO(author): would you act on this shortlist? If not, is the gap in the sourcing, the
evidence available, or the rubric?

TODO(author): the deterministic policy overruled the model on 6 of 15 candidates. Reading
those six, was the policy right each time, or is a guardrail miscalibrated?

TODO(author): where did reviewing the agent's work cost you more than writing it yourself
would have, and where did it clearly cost less?

TODO(author): which of the five constraints above would you keep if you had to drop two,
and what would you accept breaking in exchange?
