# Worklog 02 - Sourcing stage

**Stage:** discovery only. `uv run vc-scout source --query ... --limit ... --run-id ...`
**Scope as given:** HN Algolia discovery, Show HN focus, deterministic query variants,
overfetch, external-product-URL filtering, URL/domain canonicalisation, domain
deduplication, HN story retained as a source, points/comments/date captured, a transparent
discovery rank, raw + normalised + report artifacts, per-hit failure tolerance,
fixture-based offline tests. Explicitly excluded: page fetching, any LLM call.

## What was built

- `src/vc_scout/net/hn.py` - Algolia client and hit parser. The `httpx.Client` is
  injectable, which is how the tests exercise real request construction with no network.
  One retry on 429/5xx/timeouts, no retry on other 4xx. Headers are attached per request
  rather than at client construction, so an injected client still identifies itself.
- `src/vc_scout/discovery.py` - URL acceptance rules, the two domain blocklists, the
  discovery-rank formula, and HN title parsing (`"Show HN: Acme (YC W25) - does X"` ->
  name plus one-liner).
- `src/vc_scout/models/discovery.py` - the `DiscoveryRank` record.
- `src/vc_scout/models/report.py` - `SourceReport`, `VariantResult`, `DiscardedHit`.
- `src/vc_scout/stages/source.py` - the stage itself.
- `src/vc_scout/store.py` - added `raw_hn_path`, `source_report_path` and the report
  accessors.
- `src/vc_scout/cli.py` - `source` implemented; prints a funnel summary, refuses to
  overwrite an existing run without `--force`, exits 1 when nothing survives.
- `tests/fixtures/hn/*.json` - four committed Algolia responses.
- `tests/unit/{hn_fixtures,test_hn_client,test_discovery,test_source_stage}.py`.

## Artifacts written

```
outputs/runs/<run-id>/
├── raw/hn/<variant>-p0.json      verbatim Algolia responses, one per variant
├── candidates.json               normalised candidates + their HN source references
└── source-report.json            per-variant results, funnel counts, discards, failures
```

`source-report.json` is not in the artifact layout in `docs/PLAN.md`; it was added because
this stage's brief requires a sourcing report, and folding it into the run manifest would
have hidden the per-hit rejection detail.

## Two defects found by running against the live API

Both were found by reading real output, not by a test, and both now have regression tests.

1. **The first live run returned one candidate against a requested fifteen.** Algolia
   requires every query word by default, so `"AI agents for SMB operations"` on
   `tags=show_hn` matched a single story. Sending the words via `optionalWords` on three of
   the four variants took the same run from 6 hits to 271, and from 1 candidate to 15.
   Recorded as D9.
2. **The `.ai` TLD was scoring a full relevance match for the query term `ai`.** That put
   an unrelated but popular launch at the top of the ranking. The host's final label is now
   dropped before matching. Recorded as D11.

A third change came from the same reading: megacap vendor blog posts
(`deepmind.google`, `openai.com`) match a thesis query well and are uninvestable at seed,
so they are now rejected as `incumbent_domain`, a separate class from `blocked_domain`.

## Measured behaviour of the discovery rank

Over one live run of the demo query, across the 109 hits that passed URL acceptance, the
relevance component took these values:

| relevance | hits |
| ---: | ---: |
| 0.00 | 8 |
| 0.25 | 9 |
| 0.50 | 88 |
| 0.75 | 4 |

`ai` and `agents` match almost every story in this corpus; `smb` and `operations` match
almost none. So the component separates the tails but is close to constant in the middle.
It is a real improvement over ranking on engagement alone, and it is not a strong signal.
No synonym expansion was added to make it look better - see D10.

## Verification performed

| Command | Result |
| --- | --- |
| `uv run pytest` | 197 passed |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | all files formatted |
| `uv run mypy src` | no issues in 26 source files |
| live `vc-scout source --query "AI agents for SMB operations" --limit 15 --run-id source-test` | 271 hits fetched, 15 candidates kept |

Live funnel from that run: 83 discarded with no external URL, 55 blocked domains, 40
duplicate stories, 3 incumbent domains, 75 below the limit cutoff.

Three tests failed during the stage and each was a real signal rather than a flaky check:
the injected client was not sending a User-Agent (fixed in `hn.py`, not in the test); a
`VariantResult` was being mutated after construction although artifact records are frozen
(fixed by adding a mutable accumulator that seals into the record); and the CLI test still
asserted `source` was an unimplemented placeholder (replaced with real coverage).

An import cycle appeared when `Candidate` needed to carry a `DiscoveryRank` while the rank
lived in the discovery module. Resolved by moving the record into `models/discovery.py` and
leaving the formula in `discovery.py`.

## Notes for the next stage

- `Candidate.website` is a canonicalised URL and is the enrich stage's input. It is the
  story's link, which is not always the site root - some are `/blog/...` or `/onboarding`.
  Enrichment should probably fetch the origin root as well.
- Discovery rank is deliberately not persisted anywhere the policy can reach it.
- `SCHEMA_VERSION` moved to `1.1.0` when `Candidate` gained `discovery_rank`.
- The blocklists are hand-maintained and will drift. They are counted in the report so
  their effect stays visible.

## Audit of the first implementation

The first sourcing formula was run live against the demo query and its output reviewed
candidate by candidate. Objective findings:

- 15 candidates were selected against a requested limit of 15.
- The review classified **1 directly relevant, 6 adjacent and 8 likely irrelevant**.
- The only candidate whose stored source content named SMB explicitly ranked **11th**.
- **12 of the 15** candidates carried the identical relevance component value **0.50**.
  Across all 90 stories that passed URL acceptance, 71 also scored 0.50.
- There was **no minimum relevance threshold**. Relevance was only ever a weighted term,
  and 7 of the 90 accepted stories scored 0.00 relevance while remaining fully eligible.
- **All 15 selected candidates came from relaxed-word query variants.** The strict variant
  returned exactly one hit for the whole query and contributed no candidates.

Cause: relevance had insufficient resolution to compete. Measured over the 15 selected
candidates, relevance took 3 distinct values while engagement took 14, with near-identical
weighted spread (0.175 vs 0.160). In a single weighted sum the component with more
resolution decides the ordering, so engagement did. Combined with the absence of an
eligibility gate, nothing prevented a popular off-topic story from taking a shortlist place.

## Resulting technical decisions

Recorded in full as D14-D18 in `docs/DECISIONS.md`:

- **D14** - ranking is lexicographic `(relevance class, relevance score, quality score)`
  rather than a single weighted score. Engagement can no longer cross a class boundary.
  Formula version moved from `1.0.0` to `2.0.0`.
- **D15** - relevance is concept-group classification over three vocabularies (AI
  automation, business buyer, operational workflow) rather than flat query-token overlap.
  Group A is mandatory, so a non-AI business tool classifies `irrelevant`, not `direct`.
- **D16** - the query family is twelve bounded variants: three query-faithful searches plus
  nine intent facets pairing the query's AI wording with a specific buyer or workflow.
- **D17** - irrelevant and below-threshold candidates are removed before ranking and
  truncation; the shortlist fills from direct candidates first, holds adjacent ones to 30%
  while direct supply lasts, and reports a shortfall rather than padding.
- **D18** - the launch URL is preserved as a `company_page` source while `Candidate.website`
  is reduced to the site origin for enrichment.

Two further changes came out of building it. `store`/`stores` were removed from the buyer
vocabulary after "embeddings store for AI agents" classified as a retail product. The
numeric 30% adjacent cap proved provably redundant under fill-direct-first, so the code
states the policy explicitly and distinguishes `adjacent_share_reached` from
`below_limit_cutoff` in the report rather than leaving an unreachable branch.

## Test coverage added

| Guarantee | Test |
| --- | --- |
| A quiet direct workflow product outranks loud generic infrastructure | `test_a_quiet_workflow_product_outranks_loud_generic_infrastructure`, `test_class_outranks_quality_however_large_the_engagement_gap` |
| Only "AI agents" is adjacent, not direct | `test_only_ai_agents_is_adjacent_not_direct` |
| AI plus customer support is direct without saying SMB | `test_ai_plus_customer_support_is_direct_without_saying_smb` |
| AI plus accounting or scheduling is direct | `test_ai_plus_a_named_workflow_is_direct` |
| A non-AI business tool is never direct | `test_a_business_tool_without_an_ai_signal_is_never_direct`, `test_a_non_ai_business_tool_is_discarded_not_shortlisted` |
| Irrelevant removed before truncation | `test_irrelevant_candidates_are_removed_before_truncation` |
| Adjacent held to the configured share | `test_adjacent_candidates_are_capped_when_direct_supply_is_sufficient` |
| Adjacent fill when direct supply runs out | `test_adjacent_candidates_fill_the_rest_when_direct_supply_runs_out` |
| Shortfall reported, never padded | `test_a_shortfall_is_reported_rather_than_padded` |
| Engagement breaks ties within a relevance band | `test_engagement_breaks_ties_between_equally_relevant_candidates` |
| Blog launch URL kept as a source, website uses the origin | `test_a_blog_launch_url_is_kept_as_a_source_while_website_uses_the_origin` |
| No test reaches the network | `test_the_suite_genuinely_cannot_reach_the_network`, `test_the_stage_never_constructs_its_own_client` |

Committed fixtures were regenerated for the new variant labels. Shortlist-composition tests
use a synthetic corpus so the supply of direct and adjacent candidates is exact; the funnel
tests use committed JSON so they stay representative of real Algolia output.

## Verification performed

| Command | Result |
| --- | --- |
| `uv run pytest` | 227 passed |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | all files formatted |
| `uv run mypy src` | no issues in 26 source files |
| `git diff --check` | clean |

The live `source` command was not re-run for this change.

## Author's note on approving this change

I approved the relevance-first design because sourcing should answer whether a company
belongs in the investment universe before considering how popular its launch was. The
initial run showed that HN engagement could dominate weak topic relevance. I accepted a
thesis-aware vocabulary because this is a single-firm triage tool, but I kept matched terms
visible so false positives remain auditable.
