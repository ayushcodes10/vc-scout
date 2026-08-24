# Worklog 06 - Deterministic memos and the portfolio ranking

**Stage:** rendering only. `uv run vc-scout recommend --run-id <run-id>`
**Scope as given:** render partner-ready Markdown memos and a portfolio ranking from the
validated artifacts already on disk, plus a versioned recommendation report. Explicitly
excluded: the HTML UI, any API server, database, frontend framework, queue, browser
automation or vector database; any change to scoring, confidence or the recommendation
policy; any LLM or network call.

## What was built

- `src/vc_scout/render/markdown.py` - Markdown-aware neutralisation of untrusted text, and
  the two link forms the renderer is allowed to emit.
- `src/vc_scout/render/sources.py` - the deterministic source index: internal source IDs to
  reader-facing entries, with markers assigned on first use.
- `src/vc_scout/render/call.py` - the one-sentence call, its classification, and the
  reader-facing wording of every policy guardrail.
- `src/vc_scout/render/memo.py` / `ranking.py` - view models. Everything a template displays
  is computed here.
- `src/vc_scout/render/engine.py` - the Jinja environment and output normalisation.
- `src/vc_scout/templates/memo.md.j2`, `ranking.md.j2` - the two templates.
- `src/vc_scout/stages/recommend.py` - the stage: read, render, persist, record failures.
- `src/vc_scout/models/report.py` - `MemoOutcome`, `MemoFailure`, `RecommendationReport`.
- `src/vc_scout/store.py` - memo read/write/delete, memo listing, report accessors.
- `src/vc_scout/cli.py` - `recommend`, with `render` kept as a deprecated alias.

## Why rendering is deterministic rather than another LLM call

Three deterministic things already exist for every company by the time this stage runs: a
total recomputed in Python from the model's per-dimension components, a research confidence
computed from coverage facts, and a recommendation made by the policy from those two plus
its guardrails. A model asked to "write the memo" would be a fourth opinion layered on top,
free to soften a pass, dramatise a risk or restate a number it had no part in producing -
and unlike the stages upstream, nothing could catch it, because prose is not
schema-checkable.

There is a second reason, which matters more for a reviewer than for a partner: a
deterministic renderer can be *re-run as a check*. Delete `memos/` and `ranking.md`, run the
command again, and if the bytes match then the memo demonstrably is the artifacts. That
property is worth more than fluent prose. It is why there is no generated timestamp anywhere
in the output - a timestamp would break it for no informational gain, since every date a
reader actually needs (when a source was observed) is a fact about a source and is kept.

## How reader-facing citations map back to evidence

The chain that already exists is: an analysis statement cites `ev-…` claim IDs; each claim
cites `src-…` source IDs and carries a verified excerpt from each; each source has a URL.
The memo walks it and presents the far end.

Markers are assigned on first use, in reading order, so `[S1]` is whatever the top of the
document leans on first. The numbered source list at the foot contains exactly the markers
the memo spent - no more, because an uncited source is noise, and no fewer, because a marker
with no entry is a broken citation. A statement resting only on a recorded unknown gets
*Open question* instead of a marker, which is a different thing from an unattributed
sentence and reads as one.

Source display metadata is assembled with a precedence: the extracted page first, because it
records the URL the fetch actually landed on after redirects and the page's own title; then
the dossier's source table; then the discovery source table. Nothing is invented. Where no
artifact recorded a title or URL, the entry says `Source metadata unavailable`, shows the
internal identifier, and raises a report warning - the citation is degraded rather than
dropped, because dropping it would silently unanchor whatever cited it.

## What was difficult, and what changed during implementation

**Jinja's `trim_blocks` ate list separators.** The first memo template put
`{% if item.attribution %}…{% endif %}` at the end of each list item. `trim_blocks` removes
the newline after a block tag, so every risk in the list ran together onto one line - eight
bullets rendered as one paragraph. The fix was structural rather than a whitespace-control
incantation: statements expose a `.line` property that joins text and attribution in Python,
and no template contains an inline conditional. That also removed a whole class of future
whitespace bugs.

**Marker numbering followed evaluation order, not reading order.** `build_memo_view`
originally computed the scorecard rows before constructing the view, so the scorecard
claimed `[S1]` and the snapshot at the top of the document opened with `[S2] [S3]`. The
builder now computes sections in explicit document order, with a comment saying that
reordering them renumbers every citation.

**Escaping newlines out of existence welded words together.** The first neutralisation pass
dropped every control character, newlines included, before collapsing whitespace - so
`"a\nb"` became `"ab"`. Whitespace controls now become spaces and only the genuinely
invisible characters are dropped. The test that caught it was written before the code.

**The word budget was the real constraint.** The first complete memo for the densest
candidate came out at 1,206 raw tokens. Two things followed. First, raw whitespace-separated
tokens are not words a partner reads - a seven-row scorecard contributes about 150 table
pipes - so the report counts prose, excluding scaffolding, and says so. Second, per-item
budgets had to be tuned against the live artifacts rather than guessed: scorecard rationales
to 15 words, risks to 26, sections to 60, excerpts to 13, and the risks section capped at
six items. The recommendation changers are deliberately exempt: the brief says render exactly
the validated two or three without rewriting them, so they are never truncated.

**Deciding what "insufficient evidence" must never sound like.** The wording work was the
part that took longest and is the part most worth reviewing. Four situations produce a low
number, and flattening them would undo the whole pipeline: a pass because the evidence
positively places the company outside the thesis; a watch because the research established
too little to judge; a watch because a guardrail held back a score that reached higher; and
an ordinary low-fit pass. `render/call.py` classifies these and gives each its own sentence
plus a one-line "how to read this", and the tests assert the mismatch pass and the
insufficient-evidence watch are not phrased interchangeably.

## Trade-offs and remaining limitations

- **Truncation loses detail.** A long model rationale is cut with an ellipsis in the memo.
  The full text is in `analyses/<company_id>.json`, and the memo is explicitly a 60-second
  read, but a reader who only ever reads memos sees less than the artifact holds.
- **Escaping is visible in the raw Markdown.** `not\_assessable` in the source, correct when
  rendered. The alternative is trusting third-party page text with the document's structure.
- **A discovery-recorded title can be misleading.** For candidates whose website could not be
  fetched, the only title on record is the Hacker News story title, which discovery attached
  to the company URL. The memo shows it because it is what an artifact recorded; it is not
  invented, but it reads oddly next to `company page`. Fixing it belongs in sourcing, not in
  the renderer, which must not second-guess recorded metadata.
- **Near-duplicate risks are not merged.** Deduplication is exact-text only. On
  `ticketdesk-ai` a model-written risk about conflicting customer-scale claims and the
  dossier's own recorded conflict both appear, saying much the same thing in different words.
  Merging them would require judging similarity, which is a decision, and this stage does not
  make decisions.
- **`ranking.md` grows linearly.** At 15 candidates it is one screen of table. At 200 it
  would need pagination or filtering, which is what the static site is for.
- **The ordering cannot express the pass/watch asymmetry.** It is stated in prose, in the
  per-row primary rationale, and in each memo, but a reader who scans only the table still
  sees watch above pass.

## Test coverage added

`tests/unit/test_markdown_safety.py` (43), `test_memo_render.py` (25),
`test_ranking_render.py` (13), `test_recommend_stage.py` (17), plus 8 CLI tests, over a new
`tests/unit/memo_fixtures.py`.

Covered: every candidate in a 15-candidate run renders; byte-identical re-render; no
timestamp in any output; exactly seven scorecard rows summing to the recorded total; the
policy rationale verbatim; model/policy disagreement and every guardrail visible; the
zero-claim watch reading as missing evidence; the thesis-mismatch pass distinguishable from
the insufficient-evidence watch; exactly the validated two-or-three changers; every marker
resolving exactly once and numbered in reading order; uncited sources omitted; missing source
metadata warning without dropping the citation; cross-company page references refused; hostile
Markdown neutralised; every non-http URL scheme refused; no raw HTML and no embedded image in
any rendered memo; ranking sort order across all four keys; all 15 relative memo links
resolving on disk; the no-meeting explanation reconciling with the report's own counts; a
stale memo removed when this run cannot render one; the overwrite guard; and that rendering
needs no provider and no credential.

The regression tests were checked by reverting five protections in a scratch copy -
neutralisation, the URL scheme check, stale-memo cleanup, cited-only source listing and the
ranking sort - and confirming 29 tests failed.

## Verification performed

```
uv run pytest                  782 passed
uv run ruff check .            clean
uv run ruff format --check .   93 files already formatted
uv run mypy src                clean, 49 source files
git diff --check               clean
uv run vc-scout recommend --run-id source-test
```

The live render produced 15 memos, `ranking.md` and `recommendation-report.json` from the
stored `source-test` artifacts, offline. Prose word counts ranged 636-892, all inside the
900-word budget. 43 sources were cited across the 15 memos with zero missing source
metadata and zero render failures.

## Personal observations

TODO(author): your read on whether the memo is genuinely a 60-second document, or whether
the scorecard plus the risks section makes it a three-minute one in practice.

TODO(author): whether the one-sentence call is the sentence you would actually want at the
top, and whether any of the five wordings reads wrongly to you on a real candidate.

TODO(author): whether truncating model rationale at 15 words in the scorecard loses something
you would want in front of a partner, or whether the shorter table is the better artifact.

TODO(author): whether `ranking.md` sorted as a triage queue is what you want, given that it
puts four thin-evidence watches above the highest-scoring company in the run.
