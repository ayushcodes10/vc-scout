# VC Scout - run `offline-demo`

An AI-augmented investment triage run, exported for review. Everything here was produced by
`vc-scout run` from public sources; nothing in this directory was written by hand.

**Query:** AI customer support and back-office automation for small businesses
**Provider / model:** `fake` / `fake-model-1`
**Candidates:** 0 discovered, 9 analysed and written up.
**Calls:** none recorded.

## Preview the site

```bash
python3 -m http.server 8000 --directory demo/site
# then open http://127.0.0.1:8000/
```

The site is static, self-contained and offline: no build step, no framework, no CDN, no
external font or image. Opening `demo/site/index.html` directly works too, except that a
browser's file:// rules may block the stylesheet - the server above avoids that.

## What is here

| Path | What it is |
| --- | --- |
| `site/` | The browsable report: portfolio page plus one page per company |
| `ranking.md` | The same shortlist as Markdown, with the thesis and the thresholds |
| `memos/` | One partner-ready memo per company, ~700-900 words |
| `artifacts/` | Every validated artifact the run produced, in pipeline order |
| `ai-trace/` | One AI call end to end: what was sent, what came back, what was checked |

## Trace one claim back to a source

This is the property the whole pipeline exists to give you. Pick any statement in a memo:

1. Open `memos/agentkit-core.md` and find a sentence carrying a marker like `[S1]`.
2. Scroll to **Sources** at the foot of that memo. `[S1]` resolves to exactly one entry
   there, with the page title, its role, the URL and the date it was read.
3. That URL is public. Open it and check the excerpt quoted beneath the source entry.
4. To go further down: `artifacts/analyses/agentkit-core.json` shows which
   `ev-` evidence claim IDs that statement cited, and
   `artifacts/evidence/agentkit-core.json` shows each of those claims with the
   verbatim excerpt and the `src-` source it came from.

Every marker in a memo resolves to exactly one source, and every source listed is cited
somewhere above it. A statement resting only on a recorded gap is labelled *Open question*
rather than left unattributed.

## What the model did and did not decide

The model extracted evidence with verbatim citations, and wrote the narrative and the
per-dimension scores. It did **not** decide the total - that is recomputed in Python from
its own components - nor the research confidence, which is computed from coverage, nor the
recommendation, which a deterministic policy makes from the score, the confidence and a set
of guardrails. Its own suggestion is recorded next to the binding call so the two can be
compared. `ai-trace/` shows one full round trip of this.

## What is deliberately not here

No raw HTML: `raw/` holds third-party page bodies, which are the input to extraction rather
than evidence. No credentials, headers or absolute filesystem paths - the export refuses to
write if it finds any. The full run directory, including the raw pages and every persisted
request and response, stays under `outputs/runs/offline-demo/`.
