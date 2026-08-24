# One AI call, end to end

This is the analysis request and response for **n8n-io**, here so the AI workflow can be read rather than taken on trust.

## Why this candidate

Chosen by rule, not by hand: among candidates with a successful analysis and at least 3 evidence claims, the largest evidence base wins, then the highest total score, then the company ID. Every tiebreak is a recorded fact, so the same run always exports the same trace.

`n8n-io` won it with **13 evidence claims** across 5 sources, scoring 28/100. Ranking order alone would have led with a zero-evidence Watch - correct for a triage queue, useless as a worked example.

## What was sent

- provider / model: `anthropic` / `claude-sonnet-5`
- prompt version: `analysis_v2` (the system prompt is recorded by version and hash, not reproduced - it is in `src/vc_scout/prompts/`)
- thesis version: `thesis_v1`
- the user payload is the thesis, the rubric, the source-to-assessment policy and this candidate's dossier: 13 claim(s), 5 recorded unknown(s), 0 conflict(s)
- no header is recorded, so no credential can be present

## What came back, and what happened to it

- attempts: 1
- outcome: accepted
- tokens in / out: 9726 / 3099

The response is the model's raw tool call. Everything it says was checked against this candidate's dossier before anything was written:

- every cited evidence claim ID and unknown reference exists in that dossier;
- all seven rubric dimensions appear exactly once with the configured maxima;
- every score respects the ceiling its assessment status allows;
- no market-size figure appears that the evidence does not carry;
- exactly two or three recommendation changers are given.

**The model did not decide the outcome.** The total was recomputed in Python from its own per-dimension components (28/100), the research confidence was computed from coverage (0.37, low), and the recommendation - **pass** - was made by the deterministic policy. The model's own suggestion was `pass`, recorded for comparison and never consulted.

## Files

- `selected-request.json` - exactly what was sent, as persisted by the run
- `selected-response.json` - exactly what came back
- `validation-summary.md` - what was checked, and what the checks decided
