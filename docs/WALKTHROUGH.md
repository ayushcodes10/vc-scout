# Five-minute walkthrough

A timed outline for a screen recording. Talking points and things to have open, not a
script to read aloud - the numbers are the ones in the committed run, so they can be
checked while speaking.

**Have open before recording**
1. Terminal at the repository root
2. `http://127.0.0.1:8000/` — `python3 -m http.server 8000 --directory demo/site`
3. `demo/memos/n8n-io.md`
4. `demo/ai-trace/README.md`
5. `worklog/04-evidence.md`

---

## 0:00–0:30 — The problem, and the thesis

- A seed partner sees hundreds of launches a week and can read maybe ten memos.
- The scarce thing is not summarisation. It is **trust**: a memo you cannot check is a memo
  you have to redo.
- The thesis being applied is fixed, versioned and on the page:
  *seed-stage, AI-native software automating recurring, revenue-critical SMB workflows;
  value within 30 days; defensibility beyond model access.*
- **The claim to make once, up front:** every sentence in these memos traces to a public URL,
  and no model made the recommendation.

## 0:30–1:10 — One command, and where the models actually run

- Show the command. One run: source → enrich → evidence → analysis → policy → memos → site.
- Point at the README's pipeline diagram. **Two of the six stages call a model.** Discovery,
  fetching, the total, the confidence, the recommendation, the memos and the site are
  deterministic Python.
- The live run: **15 candidates, ~45 HTTP requests, ~30 model calls**, `claude-sonnet-5`.
- Running it again makes **zero** calls — every stage resumes on a fingerprint of its
  inputs, not on a file existing.

## 1:10–2:30 — One startup, from a post to verified evidence

Follow a single company (`n8n-io` is the best-evidenced; any row works).

- **Source.** Twelve deterministic Hacker News queries. Relevance is a *gate*, not a weight:
  the first live run put popular-but-irrelevant posts on top, and the fix is in
  `worklog/02-sourcing.md`.
- **Enrich.** Homepage plus up to three internal pages. SSRF-guarded, robots-respecting,
  size- and redirect-capped. A site that cannot be read keeps the candidate in the run with
  zero pages — a fact the later stages must see.
- **Evidence.** The first model call. Every claim must carry a **verbatim excerpt** from a
  supplied page, and that excerpt is checked against the source text before anything is
  written. Show a claim in `demo/artifacts/evidence/n8n-io.json`: category, verification
  status, source ID, excerpt.
- **Worth 20 seconds:** Budibase's evidence was rejected as fabricated because the page used
  a typographic apostrophe and the model returned a straight one. The fix is a seven-character
  punctuation fold, not a relaxed check (`worklog/04-evidence.md`).

## 2:30–3:30 — Analysis, the score, and the deterministic call

- **Analysis.** The second and last model call. It scores seven rubric dimensions, writes the
  narrative, and may *suggest* a recommendation.
- **What it does not decide:** the total is recomputed in Python from its own components;
  confidence is computed from coverage; the recommendation is `policy.py`.
- **In this run the policy overruled the model on 7 of the 15** (it declined to suggest
  a call on 2 more). Both are recorded
  side by side — show a "Policy override" flag on the dashboard.
- **Assessment status is about the evidence, not the company.** `not_assessable` caps what a
  dimension may score and lowers confidence; it never becomes a negative finding. The first
  calibration got this wrong — zero of 105 slots were ever `supported`, which made the
  meeting band arithmetically unreachable for everyone (`worklog/08-calibration.md`).
- **The honest headline:** no candidate reached 80. The ranking explains why from its own
  counts rather than promoting anyone to fill the band.

## 3:30–4:15 — The dashboard and one memo

- Portfolio page: run summary, filters, 15 rows in triage order. **Watch above Pass is a
  queue, not a quality ranking**, and the page says so — four of the five Watches exist
  because the research came up short, and the badge says which kind of watch it is.
- Open one memo. Recommendation, score, confidence, the one-sentence call, *why this call*
  with the policy's own rationale verbatim, the seven-dimension scorecard, sources.
- **Do the traceability demo live:** pick a scorecard row → its `[S2]` marker → the source
  entry at the foot → open that public URL → the excerpt is on the page.

## 4:15–5:00 — AI workflow, what broke, trade-offs

- `docs/AI_WORKFLOW.md`: what was decided versus implemented, and the five constraints on
  model output — strict tools, excerpt verification, evidence-ID integrity, local validation
  with one retry, deterministic scoring.
- `demo/ai-trace/`: one complete call. Request, raw response, and what validation decided —
  chosen by rule, not by hand.
- **Pick one failure and be specific.** Good options: the Anthropic *compiled grammar too
  large* 400, fixed with a compact provider schema and the full contract enforced locally;
  or the Ticketdesk conflict false positive, where a disputed customer count blocked an
  unrelated product wedge because both sat on the same page.
- Nine worklogs, written at each stage boundary, including the failures.
- **Close on the limitation, not the feature:** the evidence in this run is thin and mostly
  company-authored. The system reports that rather than dressing it up, and that is the
  behaviour worth having.
