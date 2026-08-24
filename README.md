# VC Scout

An AI-augmented investment triage pipeline for a seed-stage VC firm. It discovers
candidate startups from public sources, analyses them against a written investment
thesis, and produces a one-page memo and a ranked, browsable static report.

The design goal is not "an LLM writes memos". It is **auditability**: every analytical
claim cites an evidence ID, every evidence claim cites a source URL, the final
recommendation is made by deterministic policy rather than by a model, and a whole run
replays from persisted artifacts with no network and no API key.

> **Status: in progress.** Stages 1-5 (foundation, domain contracts, discovery, website
> enrichment, evidence extraction, scoring and the recommendation policy) are
> implemented. Memos and the static UI are not yet built.
> The pipeline commands are declared but not yet implemented - they report which stage
> owns them and exit non-zero. See `docs/PLAN.md` for the full plan and stage order.

## Quick start

```bash
uv sync
uv run vc-scout --help      # full command surface
uv run vc-scout config      # active rubric, thresholds and confidence policy
```

Evidence extraction is the only stage that needs a credential:

```bash
export ANTHROPIC_API_KEY=...   # required only for a live run
export LLM_MODEL=claude-opus-5  # optional; this is the default
```

Once the pipeline stages land, the partner-facing entry point is:

```bash
uv run vc-scout run \
  --query "AI agents for SMB operations" \
  --limit 15 \
  --run-id ai-agents-smb-demo
```

## Commands

| Command | Purpose | Status |
| --- | --- | --- |
| `source` | Discover candidates from Hacker News | available |
| `enrich` | Fetch and extract public company pages | available |
| `analyze --evidence-only` | Extract source-grounded evidence | available |
| `analyze` | Score and apply the recommendation policy | available |
| `render` | Write Markdown memos and the ranking | planned (stage 7) |
| `build-site` | Generate the static report | planned (stage 8) |
| `serve` | Serve a generated report locally | planned (stage 8) |
| `run` | Full pipeline end to end | planned (stage 9) |
| `demo` | Rebuild the committed offline demo run | planned (stage 9) |
| `config` | Show the live rubric and thresholds | available |

## Discovery

`vc-scout source` searches the public [Hacker News Algolia API](https://hn.algolia.com/api)
with a bounded, deterministic family of **twelve** queries:

| Variant | Tag | Words | Weight |
| --- | --- | --- | ---: |
| `query-show-hn` | `show_hn` | all required | 1.00 |
| `query-launch-hn` | `launch_hn` | optional | 0.85 |
| `query-story` | `story` | optional | 0.50 |
| `intent-smb` … `intent-ecommerce-retail` (9) | `(show_hn,launch_hn)` | optional | 0.90–0.95 |

The nine `intent-*` variants pair the query's own AI-automation wording with a specific
buyer or workflow — SMB, small business, business operations, customer support, sales,
finance and accounting, scheduling, back office, ecommerce and retail. They exist because
a genuinely relevant product often describes its workflow without ever using the
operator's phrasing: a scheduling agent for salons will never say "SMB operations".

Only the first variant requires every query word. The rest relax word matching, and the
recall that buys is paid for by the relevance gate below — not by hoping the query happens
to match.

### Relevance

Every story is classified from its title, one-liner, hostname and URL path against three
concept groups:

| Group | Signal | Weight |
| --- | --- | ---: |
| **A** | AI automation — `ai`, `agent`, `agentic`, `automation`, `copilot`, `assistant`, … | 0.40 |
| **B** | Business buyer — `smb`, `small business`, `merchant`, `retailer`, `contractor`, … | 0.25 |
| **C** | Operational workflow — `invoicing`, `scheduling`, `customer support`, `payroll`, `back office`, … | 0.35 |

```
relevance score = 0.40 x A + 0.25 x B + 0.35 x C     (each group: min(matches, 2) / 2)

direct      A and (B or C)   an AI product with an identifiable buyer or workflow
adjacent    A only           an AI product naming neither
irrelevant  no A             not an AI product at all
```

A business tool with no AI signal is `irrelevant`, not `direct`: groups B and C *qualify*
an AI product, they do not substitute for one. Candidates classified `irrelevant`, or
scoring below the minimum relevance of **0.20**, are discarded before anything is ranked.

### Ordering

Ranking is **lexicographic**, not a single weighted score:

```
1. relevance class      direct before adjacent
2. relevance score
3. quality score        0.55 x engagement + 0.25 x recency + 0.20 x variant weight
   engagement = log10(1 + points + 2 x comments) / log10(1 + 500), capped at 1.0
   recency    = 1.0 up to 30 days old, decaying linearly to 0.0 at 720 days
```

Engagement therefore orders candidates that are *already equally relevant* and can never
lift a generic agent-infrastructure launch above an on-topic workflow product. A single
composite score previously allowed exactly that; see D14 in `docs/DECISIONS.md`.

The shortlist is filled from directly relevant candidates first. Adjacent candidates are
held to 30% of the shortlist while direct supply lasts, and may fill the remainder only
once it runs out. **The run never pads**: if fewer defensible candidates exist than were
requested, it returns what it found and reports the shortfall.

Every component, every matched term and every rejection is recorded in
`source-report.json`, so the shortlist can be recomputed and argued with by hand.

**Discovery ranking is not investment scoring.** It runs before any page is fetched, knows
nothing about the thesis rubric, and is never read by the recommendation policy. A
top-ranked candidate can still be recommended *pass*.

## Enrichment

`vc-scout enrich` reads a bounded set of pages from each candidate's own website: the
homepage, the exact URL posted to Hacker News when it differs, and up to three internal
pages chosen deterministically by role (product, pricing, customers, about, team,
changelog, blog - at most one per role, same-origin only). Pages are deduplicated by final
URL and by content hash, reduced to readable text, and persisted with their fetch metadata
for replay.

**No candidate is ever removed for having a thin or unreachable site.** A company whose
pages could not be read keeps an empty bundle with categorised failures, so the gap stays
visible to the stages that judge it. Missing information is missing, not negative.

### Fetch safety

Every URL this stage touches came from third-party text, so all fetching goes through one
hardened client:

- http and https only, on their default ports
- hostnames resolved before connecting; loopback, private, link-local, multicast, reserved
  and unspecified addresses are refused, including IPv4-mapped IPv6 forms
- redirects followed manually so **every hop is revalidated**, capped at 3
- explicit 5s connect and 15s read timeouts
- responses abandoned mid-stream at 2 MB rather than downloaded then measured
- only `text/html` is parsed; text is capped at 20,000 characters per page
- `robots.txt` honoured; 401 and 403 respected, never worked around
- a descriptive User-Agent, and no credential, cookie or authorization header ever sent

Persisted fetch metadata contains response facts only - requested URL, final URL, redirect
chain, status, content type, content hash, byte count and timestamp. No request headers,
cookies, environment values or credentials are logged or stored.

## Evidence extraction

`vc-scout analyze --evidence-only` hands each company's own material to a language model
under a versioned prompt and writes back only what can be verified against that material.

**The model is treated as an untrusted witness.** It sees a bounded, per-candidate view -
that company's Hacker News record and its extracted pages, nothing else. It never sees
another candidate, a discovery rank, the rubric or the thesis. Structured output is
obtained with forced tool use against a fixed JSON schema.

Nothing it returns is written until it has been checked:

| Check | Rejection |
| --- | --- |
| Every cited `source_id` was supplied for *this* candidate | `unknown_source_reference` |
| Every claim carries a supporting excerpt | `schema_validation_failed` |
| Every excerpt appears in the text of the source it is attached to | `excerpt_not_found` |
| `independently_supported` is backed by two or more separate sources | `schema_validation_failed` |
| No duplicate claims | `schema_validation_failed` |

Claim identifiers are **derived** — `ev-<sha256(company_id, claim, sources)[:12]>` — never
supplied by the model, so a claim cannot be given an identity it did not earn. Invalid
output earns **exactly one retry** carrying the validation errors back; a second failure is
recorded and the run continues.

Each claim carries two independent labels: `verification_status`
(`company_claim` / `community_signal` / `independently_supported`) and `inference_status`
(`explicit` / `inferred`). Absence is first-class: `unknowns` records what the sources did
not establish and `conflicts` retains sources that disagree. **A company with no readable
website still gets a dossier** — the gap becomes unknowns, never a negative claim.

### Prompt-injection defence

Source pages are arbitrary text that any founder can edit. Two defences:

1. System instructions live in a separate channel and contain nothing about any company.
   Source text appears only in the user message, fenced in explicit
   `BEGIN/END UNTRUSTED SOURCE <id>` markers and introduced as data, never instructions.
2. **Validation makes compliance irrelevant.** A model that obeyed an injected instruction
   to invent revenue would still have to produce an excerpt, and there is no excerpt — so
   nothing is written. This is the defence that is actually tested.

### Replay

Every attempt persists a request and a response artifact under `llm/`, carrying the exact
bounded source content supplied, the prompt version and hash, the structured payload, the
validation result and errors, token usage, stop reason and latency. **No credential,
header, cookie or absolute path is ever written** — asserted by a test that greps every
artifact. A stored response can be re-validated without calling the provider.

## Analysis, scoring and the recommendation

`vc-scout analyze` reads the evidence dossiers - and **only** the dossiers. Raw pages, raw
Hacker News responses and the web are unreachable from this stage: what counts as evidence
was already decided and verified upstream.

**What the model does:** the narrative, a per-dimension assessment against the rubric, a
thesis-fit verdict, risks, open questions, and an *advisory* recommendation.

**What the model does not do:** the total (recomputed in Python from its own components),
the research confidence (computed from coverage facts), or the binding recommendation
(made by deterministic policy). Its suggestion is recorded and compared, never obeyed.

### Assessment status caps the score

The score measures **the strength of the evidence-backed investment case**, not the
company's objective worth. Every dimension carries a status, and the status caps it:

| Status | Score ceiling | Meaning |
| --- | --- | --- |
| `supported` | 100% of maximum | The evidence backs this |
| `partially_supported` | 70% | Some evidence, not enough |
| `contradicted` | 100% | The evidence shows a problem — and must say what |
| `not_assessable` | 50% | Nothing was found. **Not** a finding against the company |

`not_assessable` is deliberately neither forced to zero nor to the midpoint — the model
must choose and explain how the score reflects the uncertainty. `scored_out_of` reports how
many points were assessable, so a low total can be read correctly.

### Research confidence

Computed deterministically after the model answers, from six coverage components with
bounded penalties for identity warnings, conflicts and unknowns. **A zero-claim dossier
scores 0.0.** The full formula and its thresholds are in D30 of `docs/DECISIONS.md` and are
reproduced in `vc_scout.policy.compute_confidence`.

The `independently_supported` label earns nothing on its own — only findings the analysis
explicitly names as corroborated count (D31).

### Recommendation guardrails

Bands are `80-100` take a meeting, `65-79` watch, `0-64` pass. Then:

- a meeting also needs **medium confidence, an identifiable product and buyer, no identity
  warning, and evidence in four dimensions**;
- a pass band with **more than three unassessable dimensions and low confidence** becomes
  *watch for insufficient evidence* — unless the evidence positively shows a thesis
  mismatch, which may still pass;
- a **zero-claim dossier** becomes watch, never a fabricated score narrative;
- an **unresolved cross-domain identity mismatch** caps at watch;
- a **missing website never forces a pass** on its own.

Every guardrail that fires is named in the artifact, next to the band it moved from, the
model's suggestion, and whether the two disagreed.

## Investment thesis

We invest in seed-stage, AI-native software companies that automate recurring,
revenue-critical workflows for SMBs. The product should produce measurable value within
30 days, integrate into an existing system of record, and develop defensibility through
proprietary workflow data, distribution, integrations or operational depth rather than
relying only on model access.

## Scoring

| Dimension | Points |
| --- | ---: |
| Pain and measurable ROI | 20 |
| Product wedge | 15 |
| Distribution | 15 |
| Defensibility | 15 |
| Team | 15 |
| Traction and freshness | 10 |
| Market and timing | 10 |
| **Total** | **100** |

`80-100` take a meeting, `65-79` watch, `0-64` pass.

**Research confidence is separate from the investment score.** The score answers "how
well does this fit the thesis?"; confidence answers "how much did we actually find out?".
Missing information is recorded as *unknown*, never as a negative judgment - it leaves a
dimension unscored and lowers confidence, and low confidence caps the recommendation at
*watch*.

## Development

```bash
uv run pytest          # offline, no API key required
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

## Documentation

- `docs/PLAN.md` - architecture, artifact contracts, stage boundaries, non-goals
- `docs/DECISIONS.md` - design decisions and their trade-offs
- `worklog/` - incremental implementation notes

## AI assistance

This repository was built with AI assistance. How it was used is disclosed in
`docs/AI_WORKFLOW.md` (added in a later stage). All commits are authored by the
repository owner.
