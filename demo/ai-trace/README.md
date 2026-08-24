# One AI call, end to end

This is the analysis request and response for **agentkit-core** - the highest-ranked candidate in this run with a successful analysis. It is here so the AI workflow can be read rather than taken on trust.

## What was sent

- provider / model: `fake` / `fake-model-1`
- prompt version: `analysis_v2` (the system prompt is recorded by version and hash, not reproduced - it is in `src/vc_scout/prompts/`)
- thesis version: `thesis_v1`
- the user payload is the thesis, the rubric, the source-to-assessment policy and this candidate's dossier: 4 claim(s), 1 recorded unknown(s), 0 conflict(s)
- no header is recorded, so no credential can be present

## What came back, and what happened to it

- attempts: 1
- outcome: accepted
- tokens in / out: 1539 / 64

The response is the model's raw tool call. Everything it says was checked against this candidate's dossier before anything was written:

- every cited evidence claim ID and unknown reference exists in that dossier;
- all seven rubric dimensions appear exactly once with the configured maxima;
- every score respects the ceiling its assessment status allows;
- no market-size figure appears that the evidence does not carry;
- exactly two or three recommendation changers are given.

**The model did not decide the outcome.** The total was recomputed in Python from its own per-dimension components (41/100), the research confidence was computed from coverage (0.55, medium), and the recommendation - **pass** - was made by the deterministic policy. The model's own suggestion was `watch`, recorded for comparison and never consulted.

## Files

- `selected-request.json` - exactly what was sent, as persisted by the run
- `selected-response.json` - exactly what came back
- `validation-summary.md` - what was checked, and what the checks decided
