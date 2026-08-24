# Worklog 09 - One command, and a directory a reviewer can open

**Stage:** orchestration and export. `vc-scout run`, `vc-scout demo`, `vc-scout export-demo`.
**Scope as given:** a thin orchestrator over the existing stage APIs, a run report, correct
stop/continue semantics, resume with fingerprints, an offline fixture-backed demo, and a
reviewer-ready export. Explicitly excluded: reimplementing any stage, and running the live
Anthropic demo.

## What was built

- `src/vc_scout/pipeline.py` - the orchestrator. It owns order, resume and stop/continue,
  and nothing else; every stage is called through its existing entry point.
- `src/vc_scout/fingerprint.py` - canonical-projection hashes over each stage's inputs.
- `src/vc_scout/demo_fixtures/` - `hn.json`, `pages.json` and the loaders that put the
  production HTTP and Algolia clients over them.
- `src/vc_scout/stages/export.py` - the `demo/` assembler, with a refuse-rather-than-ship
  scan.
- `src/vc_scout/models/report.py` - `RunReport`, `StageRun`, and an `upstream_fingerprint`
  on all five derived reports.
- `src/vc_scout/cli.py` - `run`, `demo`, `export-demo`, with every option validated before a
  directory is created.
- `tests/integration/` - 30 tests over the whole pipeline and the export.

## The orchestration is deliberately thin

The orchestrator holds three things: which stage follows which, whether a stage's artifacts
are current enough to skip, and which failures end a run. It contains no sourcing, no
fetching, no extraction, no scoring and no rendering. Adding any would have been the bug -
two copies of a stage drift, and the drift is invisible until an artifact disagrees with the
report describing it.

The one thing it does *to* a stage is stamp the fingerprint of that stage's inputs onto the
report afterwards. That lives here rather than inside each stage so the stages stay unaware
of orchestration, and so an artifact written by a single-stage command carries no
fingerprint - which a later resume reads, correctly, as unknown provenance.

## Resume: fingerprints, not file existence

`if the report exists, skip the stage` is the obvious design and it is wrong. It quietly
becomes "render last week's analysis over this week's evidence", and nothing in the output
says so - which breaks the one property this whole project is built on.

Each derived report records a SHA-256 over a canonical projection of what it was derived
from. A stage resumes only when its report exists, validates, covers every current
candidate, **and** carries the fingerprint of the input as it stands now.

Timestamps are excluded: a rerun producing identical evidence one second later has not
changed the input. The projections record identity and shape - which companies, which
claims, which statuses - not prose, so a reworded rationale does not invalidate the memo
built on it, while a claim appearing or disappearing does.

Forcing a stage invalidates it and everything downstream, never anything upstream. And once
a stage actually runs, everything below it is invalidated automatically - which is what
stops a fresh analysis from being published under yesterday's memos.

## Failure semantics

| What happens | What the run does |
|---|---|
| Discovery fails outright | stops - nothing downstream is meaningful |
| Discovery finds nobody | stops, with the funnel recorded |
| A site cannot be read | continues; the candidate stays in the run with zero pages |
| Extraction or analysis fails for one candidate | continues; recorded per candidate |
| The provider fails at run level (401, 400, missing key) | evidence and analysis stop; the run records why |
| A memo cannot be rendered | continues; its stale memo is removed and the failure recorded |
| No analysis report exists at all | recommend and ui are skipped, not failed |

The distinction that matters is between a candidate failing and a run failing. A 401 means
every remaining request fails identically, so spending them is waste - the run stops after
one. A company with an unreachable website is a fact about the research, and the pipeline
was built to carry it forward rather than drop the company.

## What was difficult

**The fake provider produced no evidence, so the demo produced nothing.** `FakeProvider`
returned an empty evidence payload by design - "asserts nothing about any company" - which
meant zero claims, zero unknowns, unanchorable analysis sections, and a demo where all nine
candidates failed validation twice. It now quotes its supplied sources verbatim: every claim
is "the supplied pricing page states: <span copied from that page>". That keeps it honest -
the excerpt is checked by the same verifier that catches a real model inventing a quotation -
while inventing no fact. The demo went from nine failures to nine memos.

**Resume needed a definition of "current" before it could be written.** The first sketch
compared file mtimes, which is wrong twice over: a copied directory has new mtimes and
unchanged content, and a rewritten file has a new mtime and identical content. Hashing the
inputs is the only version of the question that has an answer.

**Typer's option defaults fight ruff.** `typer.Option(...)` in an argument default is a
function call in a default, which `B008` refuses. Module-level singletons satisfy both.

## Trade-offs and limitations

- **An artifact written by a single-stage command is never resumed.** It costs a rerun on a
  mixed workflow. It is the right direction to fail in.
- **The fingerprints are shallow by design.** They would not notice a page's *text* changing
  while its content hash stayed the same, which cannot happen, or a model rewording a
  rationale, which is deliberate.
- **`run --provider fake` still uses the real network** for sourcing and enrichment. Only
  `demo` is fully offline. Pretending the fake provider covered HTTP is exactly how a test
  ends up making a request nobody expected.
- **The demo fixtures are hand-written.** They exercise the pipeline; they are not a real
  market, and the fake provider quotes rather than reasons, so demo scores show the
  mechanism and not a judgement.
- **The export ships one AI call, not all of them.** Chosen deterministically as the
  highest-ranked successful candidate. The full set stays in the run directory.
- **`serve` is still unimplemented.** `python3 -m http.server` does the job and the README
  says so.

## Offline demo and export

```
uv run vc-scout demo --run-id offline-demo
  [part] source     ran   0.01s  kept 9 of a requested 10; 1 place(s) unfilled
  [ok  ] enrich     ran   0.03s  27 page(s) read; 0 candidate(s) with no readable page
  [ok  ] evidence   ran   0.01s  all 9 candidate(s) succeeded
  [ok  ] analysis   ran   0.01s  all 9 candidate(s) succeeded
  [ok  ] recommend  ran   0.02s  9 of 9 memo(s) rendered
  [ok  ] ui         ran   0.03s  10 page(s) generated
```

Repeated, all six stages resume and nothing is called. `export-demo` wrote 63 files to
`demo/`: 9 memos, 10 site pages, 12 artifact documents plus the per-company directories, and
one AI call end to end. 73 internal links checked, none broken. No raw HTML, no
credential-shaped string, no absolute path.

One candidate of the ten was dropped by the relevance gate rather than padded into the
shortlist, which is the sourcing stage behaving as designed.

## Verification

```
uv run pytest                  911 passed (39 new)
uv run ruff check .            clean
uv run ruff format --check .   clean
uv run mypy src                clean, 56 source files
git diff --check               clean
```

No external request was made during implementation. Every network client in the tests is a
production client over `httpx.MockTransport`, and `tests/conftest.py` blocks sockets and DNS
outright, so a regression that reintroduced a live call fails loudly.

## Personal observations

TODO(author): whether resume is the behaviour you want by default, or whether you would
rather `run` always rebuilt and made you ask for resume explicitly.

TODO(author): whether the demo fixtures should look more like the real Show HN corpus -
messier, thinner, with a couple of dead sites - given that a clean corpus makes the pipeline
look better than a real run does.

TODO(author): whether one AI call in `demo/ai-trace/` is enough to show the workflow, or
whether a reviewer would want the retry case as well.

TODO(author): what you want `demo/` to hold when the live run replaces it - the live run
only, or both.
