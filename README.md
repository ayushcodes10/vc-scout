# VC Scout

An AI-augmented investment triage pipeline for a seed-stage VC firm. It discovers
candidate startups from public sources, analyses them against a written investment
thesis, and produces a one-page memo and a ranked, browsable static report.

The design goal is not "an LLM writes memos". It is **auditability**: every analytical
claim cites an evidence ID, every evidence claim cites a source URL, the final
recommendation is made by deterministic policy rather than by a model, and a whole run
replays from persisted artifacts with no network and no API key.

> **Status: in progress.** Stages 1-3 (foundation, domain contracts, discovery and
> website enrichment) are implemented.
> The pipeline commands are declared but not yet implemented - they report which stage
> owns them and exit non-zero. See `docs/PLAN.md` for the full plan and stage order.

## Quick start

```bash
uv sync
uv run vc-scout --help      # full command surface
uv run vc-scout config      # active rubric, thresholds and confidence policy
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
| `analyze` | Extract evidence, score, apply policy | planned (stages 4-6) |
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
