# VC Scout

An AI-augmented investment triage pipeline for a seed-stage VC firm. It discovers
candidate startups from public sources, analyses them against a written investment
thesis, and produces a one-page memo and a ranked, browsable static report.

The design goal is not "an LLM writes memos". It is **auditability**: every analytical
claim cites an evidence ID, every evidence claim cites a source URL, the final
recommendation is made by deterministic policy rather than by a model, and a whole run
replays from persisted artifacts with no network and no API key.

> **Status: in progress.** Stage 1 (foundation and domain contracts) is implemented.
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
| `source` | Discover candidates from Hacker News | planned (stage 2) |
| `enrich` | Fetch and extract public company pages | planned (stage 3) |
| `analyze` | Extract evidence, score, apply policy | planned (stages 4-6) |
| `render` | Write Markdown memos and the ranking | planned (stage 7) |
| `build-site` | Generate the static report | planned (stage 8) |
| `serve` | Serve a generated report locally | planned (stage 8) |
| `run` | Full pipeline end to end | planned (stage 9) |
| `demo` | Rebuild the committed offline demo run | planned (stage 9) |
| `config` | Show the live rubric and thresholds | available |

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
