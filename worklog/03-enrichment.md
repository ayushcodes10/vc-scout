# Worklog 03 - Bounded website enrichment

**Stage:** enrichment only. `uv run vc-scout enrich --run-id <run-id>`
**Scope as given:** fetch each candidate's canonical website, discover relevant internal
links, read the homepage plus at most three internal pages, extract text and metadata,
deduplicate, record failures per candidate without aborting the run, persist `raw/web/`,
`extracted/` and `enrichment-report.json`. Explicitly excluded: LLM calls, evidence
extraction, scoring, memos, UI, database, browser automation.

## What was built

- `src/vc_scout/net/http.py` - `SafeFetcher`. All fetch-time security lives here: scheme
  and port allow-list, DNS resolution with a public-address check, manual redirect
  following so every hop is revalidated, streaming byte ceiling, `text/html` enforcement
  and robots.txt. The `httpx.Client` and the DNS resolver are both injectable, which is how
  the tests exercise the real code offline.
- `src/vc_scout/extract.py` - HTML to text, and deterministic internal-link selection.
  Chrome (`nav`, `header`, `footer`, `aside`) and noise (`script`, `style`, `iframe`,
  `form`) are removed, then `<main>`/`<article>`/`[role=main]`/`<body>` is used as the root
  in that order. Title, headings and block text are kept in document order, whitespace
  normalised, repeats collapsed, bounded at 20,000 characters.
- `src/vc_scout/stages/enrich.py` - the stage.
- `src/vc_scout/models/page.py` - `ExtractedPage` extended with `final_url`, `content_type`,
  `fetched_at`, `content_sha256`, `headings`, `role`, `truncated`, `body_truncated`;
  `PageFailure` added; `PageBundle` gained `status` and `failures`.
- `src/vc_scout/models/enums.py` - `PageRole`, `FetchFailure`, `EnrichmentStatus`.
- `src/vc_scout/models/report.py` - `EnrichmentReport`, `CandidateEnrichment`.
- `src/vc_scout/store.py` - `raw_web_path`, `enrichment_report_path`,
  `extracted_company_ids` and the report accessors.
- `src/vc_scout/cli.py` - `enrich` implemented, with a `--force` guard.
- `beautifulsoup4` added as a dependency.

## Enrichment flow

Per candidate: fetch `Candidate.website` as the homepage; if the HN launch URL differs from
it, queue that next; select up to the remaining allowance of internal links from the
homepage HTML; fetch the queue. The launch URL takes one of the three additional slots
rather than adding a fourth page. If the homepage is unreadable the launch page becomes the
link seed instead, so a company is not lost to one bad root document.

Link selection is deterministic: same-origin only, matched against a fixed role priority
(product, pricing, customers, about, team, changelog, blog), at most one page per role,
ties broken by path depth then path length then alphabetically.

Deduplication happens on three keys: the requested URL, the final URL after redirects, and
the SHA-256 of the response body.

## Page-role priority

Ordered product, pricing, customers, about, team, changelog, blog. Pricing and customers
sit high because they carry the clearest evidence of who pays and for what. Blog sits last
because it is usually the largest and least specific surface on a startup site, and the
per-page text cap makes a long blog post expensive in extracted characters.

## Security controls implemented

| Control | Where |
| --- | --- |
| http/https only, default ports only | `SafeFetcher.validate` |
| Loopback, private, link-local, multicast, reserved, unspecified addresses refused | `is_public_address` |
| IPv4-mapped IPv6 unwrapped before checking | `is_public_address` |
| Hostname resolved before connecting; every resolved address checked | `SafeFetcher.validate` |
| Every redirect hop revalidated | `SafeFetcher._open` |
| Redirects capped (default 3) | `SafeFetcher._open` |
| Explicit connect (5s) and read (15s) timeouts | `SafeFetcher` defaults |
| Response abandoned mid-stream at 2 MB | `SafeFetcher._read` |
| Only `text/html` and `application/xhtml+xml` parsed | `SafeFetcher.fetch_html` |
| Descriptive User-Agent, no credentials sent | `net/http._HEADERS` |
| robots.txt honoured, fetched once per host | `SafeFetcher.allowed_by_robots` |
| 401/403 treated as a decision to respect, never retried | `SafeFetcher._open` |
| Per-host politeness delay | `SafeFetcher._throttle` |
| Company IDs validated before use as path segments; URLs stored as digests | `RunStore.raw_web_path` |

No request header, cookie, credential or environment value is sent, logged or persisted.
The stored metadata contains only response facts: requested URL, final URL, redirect chain,
status, content type, content hash, byte count, truncation flag and timestamp.

## Objective notes from implementation

Two defects were found by tests during the stage and fixed in the code rather than the test:

1. `select_internal_links` checked its limit *after* appending, so `limit=0` returned every
   matched role instead of nothing.
2. Pydantic computed fields (`char_count`, `total_chars`) were serialised on write but
   rejected on read by `extra="forbid"`, so `extracted/*.json` did not round-trip. Fixed
   generically with a `mode="before"` validator on `RecordModel` that drops computed keys.

Three test failures were defects in the tests themselves: a truncation fixture used 3,000
identical paragraphs that the repeat-collapsing logic correctly reduced to one; a route map
served the same body at three paths, which content-hash deduplication correctly collapsed;
and a helper treated an explicitly empty route map as "use the defaults".

The live enrichment command was not run during this stage.

## Test coverage added

| Guarantee | Test |
| --- | --- |
| Successful HTML extraction | `test_meaningful_paragraphs_and_list_items_survive` |
| Title and heading extraction | `test_the_page_title_is_extracted`, `test_headings_are_extracted_in_document_order` |
| Relevant link selection | `test_relevant_links_are_selected_in_priority_order` |
| Same-origin enforcement | `test_only_same_origin_links_are_followed`, `test_a_protocol_relative_link_to_another_host_is_not_same_origin` |
| Maximum page count | `test_the_page_limit_is_respected`, `test_never_more_than_three_pages_beyond_the_homepage` |
| Redirect validation | `test_redirects_are_followed_and_the_final_url_is_recorded`, `test_redirect_chains_are_capped` |
| Redirect to a private address rejected | `test_every_redirect_hop_is_revalidated` |
| Initial private address rejected | `test_an_initial_private_address_is_rejected_before_any_request` |
| DNS resolving to a private address | `test_dns_resolving_to_a_private_address_is_rejected` |
| Non-HTML rejection | `test_only_html_is_parsed` |
| Streaming size limit | `test_an_oversized_response_is_cut_while_streaming` |
| Timeout | `test_a_timeout_is_categorised` |
| Connection failure | `test_a_connection_failure_is_categorised` |
| Duplicate final URLs | `test_pages_redirecting_to_the_same_final_url_are_stored_once` |
| Duplicate content hashes | `test_pages_with_identical_content_hashes_are_stored_once` |
| Candidate-level failure isolation | `test_one_failing_candidate_does_not_fail_the_run`, `test_an_unexpected_client_fault_is_contained_to_one_candidate` |
| Partial candidate success | `test_partial_success_is_distinguished_from_success_and_failure` |
| Safe persistence, no headers or secrets | `test_persisted_metadata_never_contains_headers_or_secrets` |
| `--force` before overwrite | `test_enrich_requires_force_before_overwriting` |

## Verification performed

| Command | Result |
| --- | --- |
| `uv run pytest` | 340 passed |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | all files formatted |
| `uv run mypy src` | no issues in 29 source files |
| `git diff --check` | clean |

## Notes for the next stage

- `PageBundle` is written for every candidate, including those with zero pages. The evidence
  stage must treat an empty bundle as missing information, not as a negative signal.
- `ExtractedPage.text` is untrusted third-party content and is the input the evidence prompt
  will see. It must be delimited and framed as data, never as instructions.
- The launch URL and the site origin are separate `SourceReference` records with separate
  IDs, so a claim can cite the exact page it came from.

## Live run and the defect it exposed

The first live enrichment run over `source-test` produced **11 complete, 1 partial and 3
failed** candidates: 33 pages attempted, 29 extracted, 3 deduplicated, 78,713 characters,
with 1 connection error, 1 HTTP error and 2 timeouts. Every one of those figures reconciles
against the persisted bundles.

Auditing the artifacts surfaced an implementation defect. Link discovery passed
`Candidate.website` - the *requested* homepage URL - to `select_internal_links`, rather than
the URL the homepage was actually served from. When a homepage redirects, relative links in
the response body are relative to the final URL, so resolving them against the requested URL
produces addresses the site never published, and scopes the same-origin filter to a host the
document no longer belongs to.

Two candidates were affected in the live run:

- **agentic-commits** - `https://agentic-commits.deligoz.me/` redirected to
  `https://deligoz.me/projects/agentic-commits/`. The follow-up link resolved to
  `https://agentic-commits.deligoz.me/articles` instead of `https://deligoz.me/articles`,
  which timed out. This was the sole cause of the run's only partial result.
- **gibsonai-com** - `https://www.gibsonai.com/` redirected to `https://memorilabs.ai/`.
  Follow-up links resolved against the abandoned host, which still forwards, so all three
  pages were retrieved but each cost an extra redirect hop.

## Fix

`_fetch_and_extract` now returns `(final_url, html)` instead of the HTML alone, and
`_enrich_candidate` passes that final URL as `base_url` to `select_internal_links`. Because
`select_internal_links` uses `base_url` both to resolve relative hrefs and to enforce
same-origin, one change corrects resolution and origin scoping together. The seed page's
final URL is also added to the exclusion set, so a redirect target carrying a path cannot be
selected as its own follow-up.

Nothing else changed. `SafeFetcher` validation and redirect revalidation are untouched, no
extraction, sourcing or model behaviour was modified, and no artifact schema changed. The
requested URL, final URL and full redirect chain remain persisted in both
`extracted/<company_id>.json` and `raw/web/<company_id>/*.meta.json`.

## Regression tests for the fix

Seven tests were added to `tests/unit/test_enrich_stage.py`. Five of them fail against the
pre-fix source and pass after it; the remaining two are invariance tests that must pass in
both states, which is their purpose.

| Test | Proves | Fails pre-fix |
| --- | --- | --- |
| `test_relative_links_resolve_against_the_final_host_after_a_redirect` | Cross-host redirect rebases relative links | yes |
| `test_the_stale_host_is_never_requested_for_a_follow_up_page` | The abandoned host receives no follow-up request | yes |
| `test_same_origin_filtering_uses_the_final_host` | An explicit anchor back to the original host is now cross-origin | yes |
| `test_a_same_host_redirect_to_another_base_path_rebases_relative_links` | The agentic-commits case: same host, deeper base path | yes |
| `test_the_seed_page_is_not_reselected_as_its_own_follow_up` | A redirect target with a path is not fetched twice | yes |
| `test_the_requested_url_and_redirect_chain_stay_persisted` | Traceability is unaffected by the fix | no (invariant) |
| `test_link_resolution_is_unchanged_when_nothing_redirects` | The non-redirect path is untouched | no (invariant) |

`tests/fixtures/web/relative-links.html` was added: a page using document-relative hrefs, so
a base-path change actually alters resolution.

Live enrichment was not re-run for this fix.

## Personal observations

TODO(author): whether the four-page ceiling is the right trade for a seed-stage triage tool,
or whether pricing and customers pages alone would carry most of the signal.

TODO(author): your read on the extraction quality once you have seen it run against real
sites, particularly on JavaScript-rendered pages where this approach recovers little.
