# VC Scout — Implementation Plan

> Status: agreed before implementation began. This document is the plan of record.
> Sections 1-12 are design decisions. Anything requiring first-person judgment is marked
> `TODO(author)` and is to be written by the repository author, not generated.

## Context

The repository is empty (initial commit only: `README.md`, `.gitignore`). We are building **VC Scout**, a take-home deliverable: an AI-augmented investment triage pipeline for a seed-stage VC firm.

A partner runs one command, and gets back a ranked set of 10–20 startups discovered from Hacker News, each with a one-page memo scored against a written thesis, plus a browsable static site. The hard requirement is not "an LLM writes memos" — it is **auditability**: every analytical claim traces to an evidence ID, every evidence item traces to a source URL, the LLM never makes the recommendation, and the whole run replays from persisted artifacts with no network and no API key.

The intended outcome is a repo a partner can trust and a reviewer can verify: deterministic where it matters, honest about what it does not know, and transparent about how it was built.

**Decisions confirmed before implementation:** Anthropic as the single configurable provider; a fixture-generated `demo` run committed to the repo; a minimal GitHub Actions CI workflow.

TODO(author): why this problem framing was chosen, and what you would have scoped differently given more than eight hours.

**Working constraints for this project:** I never commit or push. Every implementation stage ends with a report (files changed / tests executed / test results / unresolved concerns / suggested commit message) and the user commits manually. No fabricated history, failures, or reflections anywhere — `TODO(author)` markers wherever first-person judgment belongs.

---

## 1. Repository structure

```
vc-scout/
├── pyproject.toml              # uv project, [project.scripts] vc-scout, ruff+mypy+pytest config
├── uv.lock  .python-version  README.md  .gitignore
├── .github/workflows/ci.yml    # ruff · mypy · pytest (offline)
├── config/
│   ├── thesis.md               # the investment thesis, verbatim, content-hashed into manifests
│   └── rubric.py               # dimension weights as typed constants (single source of truth)
├── src/vc_scout/
│   ├── cli.py                  # Typer app: source enrich analyze render build-site serve run demo
│   ├── config.py               # Settings: provider, model, run root, limits, timeouts (env + flags)
│   ├── store.py                # RunStore — the ONLY module that touches run paths; atomic writes
│   ├── models/                 # candidate.py evidence.py analysis.py memo.py manifest.py page.py
│   ├── net/
│   │   ├── http.py             # httpx client: timeouts, retries, size caps, SSRF guard, robots
│   │   └── hn.py               # HN Algolia search client
│   ├── stages/
│   │   ├── source.py  enrich.py  extract.py  analyze.py
│   │   ├── policy.py           # deterministic recommendation + confidence — NO LLM, NO network
│   │   ├── render.py           # Jinja2 → memos/*.md, ranking.md
│   │   └── site.py             # Jinja2 → site/
│   ├── llm/
│   │   ├── provider.py         # LLMProvider protocol + LLMResult + call logging/redaction
│   │   ├── anthropic.py        # httpx → Messages API, forced tool-use for schema-locked JSON
│   │   ├── fake.py             # deterministic offline provider (tests + demo)
│   │   ├── validate.py         # schema + citation + market-number validators, retry-once driver
│   │   └── prompts/extract_evidence.v1.md, analyze_company.v1.md
│   ├── util/                   # ids.py (slugs) urls.py (scheme/host safety) text.py jsonio.py
│   └── templates/
│       ├── memo.md.j2  ranking.md.j2
│       └── site/base.html.j2 index.html.j2 company.html.j2 methodology.html.j2
│           └── assets/app.css  app.js
├── tests/{unit,golden,e2e}/ + fixtures/ (HN JSON, saved HTML pages, LLM responses)
├── docs/DECISIONS.md  AI_WORKFLOW.md  EVALUATION.md  LIMITATIONS.md  llm-samples/
├── worklog/0001-*.md …          # one entry per stage, written at the stage boundary
└── outputs/runs/                # demo/ committed; all other run-ids gitignored
```

## 2. Domain models (Pydantic v2, all `frozen=True`, all carry `schema_version`)

| Model | Key fields | Invariants enforced by validators |
|---|---|---|
| `SourceRef` | `source_id` (`src-001`), `url`, `title`, `domain`, `kind` (`hn_story`\|`hn_comments`\|`company_page`), `fetched_at`, `published_at?`, `hn_points?` | URL is http/https, host is public |
| `Candidate` | `company_id` (slug), `name`, `one_liner`, `website?`, `source_ids[]`, `query`, `discovered_at` | `company_id` matches `^[a-z0-9][a-z0-9-]{0,63}$`; ≥1 source |
| `RawDocument` | `company_id`, `url`, `status`, `content_type`, `sha256`, `bytes`, `body_path` (relative) | path is relative, inside run dir |
| `ExtractedPage` | `company_id`, `source_id`, `title`, `text`, `chars`, `extractor` | text truncated to cap |
| `Evidence` | `evidence_id` (`ev-001`), `category` (7 rubric dims + `general`), `claim`, `label` (`company_claim`\|`third_party`\|`inference`), `source_ids[]`, `quote?` | `source_ids` non-empty **and** all resolve; `quote` must be a substring of the cited page text |
| `EvidenceBundle` | `company_id`, `evidence[]`, `sources[]`, `prompt_version`, `model`, `created_at`, `warnings[]` | evidence IDs unique |
| `DimensionScore` | `key`, `score` (0..max) \| `None`, `max`, `rationale`, `evidence_ids[]`, `status` (`scored`\|`unknown`) | `status=scored` ⇒ ≥1 resolving evidence ID; `unknown` ⇒ `score is None` |
| `Analysis` | 7 `DimensionScore`s, `narrative.{team,product,market}`, `risks[]`, `open_questions[]`, `what_would_change[]` (2–3), `plain_language_product`, `total_score`, `scored_out_of`, `llm_self_confidence` | `total_score` **recomputed in Python**, never trusted from the model; every narrative/risk item cites ≥1 evidence ID; **any `recommendation` key from the model is a hard schema rejection** |
| `Confidence` | `level` (`low`\|`medium`\|`high`), `score` 0–1, `components{}`, `reasons[]`, `missing[]` | computed deterministically, never by LLM |
| `Recommendation` | `decision` (`take_a_meeting`\|`watch`\|`pass`), `band`, `capped`, `cap_reason?`, `policy_version` | derived only from `total_score` + `Confidence` |
| `RunManifest` | run id/query/limit, thesis SHA-256, rubric + policy versions, prompt versions + hashes, provider/model, per-stage `StageRecord`s (status, counts, duration, inputs read, outputs written), per-company status table, `failures[]`, LLM call/token totals | no absolute paths, no secrets |

## 3. Artifact contracts

```
outputs/runs/<run-id>/
├── raw/hn/<query-sha1>-p<n>.json          # verbatim Algolia responses
├── raw/pages/<company_id>/<url-sha1>.html + .meta.json
├── raw/llm/<company_id>-<stage>-<n>.json  # redacted request + full response + timing/tokens
├── candidates.json                        # {schema_version, run, candidates:[Candidate]}
├── extracted/<company_id>.json            # {company_id, pages:[ExtractedPage], sources:[SourceRef]}
├── evidence/<company_id>.json             # EvidenceBundle
├── analyses/<company_id>.json             # {analysis: Analysis, confidence: Confidence, recommendation: Recommendation}
├── memos/<company_id>.md
├── ranking.md
├── run-manifest.json
└── site/index.html  methodology.html  companies/<company_id>.html  assets/{app.css,app.js}
```

Rules: JSON written with `sort_keys`, `indent=2`, trailing newline (diffable); atomic `tmp → os.replace`; every write goes through `RunStore`, which resolves and asserts the path is inside the run directory. `analyses/` holds the policy output alongside the analysis because the required directory list has no `policy/` — the boundary stays visible as separate top-level keys and separate `StageRecord`s.

## 4. Stage boundaries

Every stage reads persisted input and writes persisted output; every stage is independently re-runnable, skips completed work unless `--force`, and records a `StageRecord`.

| # | Stage | Reads | Writes | Network | LLM |
|---|---|---|---|---|---|
| 1 | `source` | query, limit | `raw/hn/`, `candidates.json` | HN Algolia | no |
| 2 | `enrich` | `candidates.json` | `raw/pages/`, `extracted/` | company sites | no |
| 3 | `extract` | `extracted/` | `evidence/` | no | **yes (call 1)** |
| 4 | `analyze` | `evidence/`, thesis, rubric | `analyses/` (analysis) | no | **yes (call 2)** |
| 5 | `policy` | `analyses/`, evidence coverage | `analyses/` (confidence + recommendation) | no | no |
| 6 | `render` | `analyses/`, `evidence/`, `candidates.json` | `memos/`, `ranking.md` | no | no |
| 7 | `build-site` | all of the above + manifest | `site/` | no | no |

CLI mapping: `vc-scout analyze` runs 3→4→5 (with `--only extract|analyze|policy` for debugging); `run` runs 1→7; `demo` runs 1→7 against bundled fixtures with `--provider fake --offline`, producing a byte-stable committed run; `serve` starts `http.server` bound to `127.0.0.1` over `site/`.

**Sourcing detail:** HN Algolia `search` + `search_by_date` across query variants (raw query, `Show HN <query>`, `Launch HN <query>`), story tags, ~last 24 months. Candidates come from stories that carry an external URL; news/aggregator/social domains are dropped; dedupe by registrable domain then normalized name; take top `--limit` by a simple recency×engagement blend. If fewer than 10 survive, widen the date window once, then report the shortfall in the manifest rather than padding.

**Enrichment detail:** per company, homepage + up to 3 discovered same-host pages matching `/about|/pricing|/customers|/product`, plus the HN comment thread (valuable third-party signal). Caps: 2 MB/response, ≤3 redirects (each re-validated), 10 s read timeout, ≤4 concurrent hosts, robots.txt honored, identifying User-Agent. Trafilatura for extraction, BeautifulSoup fallback.

## 5. LLM boundaries

Exactly **two** call sites, both schema-locked, both behind `LLMProvider.complete_json(system, user, schema, …) -> LLMResult`.

**The LLM may:** extract evidence with source citations and claim labels; write team/product/market narrative; assign per-dimension scores with rationale + evidence IDs; propose risks, open questions, and the 2–3 things that would change the call; write the plain-language product explanation.

**The LLM may not:** choose the recommendation (schema rejects the field); compute the total (recomputed in Python from dimension scores); set research confidence (deterministic); cite an ID outside the provided set (validated, unknown IDs stripped and the item demoted to `unknown`); state a market-size figure not present in a cited quote (regex scrubber for currency/scale patterns → claim rejected); touch the network or filesystem.

Fetched page text is untrusted input: delimited, explicitly framed as data-never-instructions, and no model output is ever executed or interpolated as code/markup.

Retry policy: **at most one retry per call**, either a repair attempt carrying the validator errors back, or one backoff retry on 429/5xx/timeout — never both, never twice. Exhausted ⇒ that company is marked failed for that stage and the run continues.

Versioning: prompts are files with SHA-256 recorded in the manifest; `MODEL_ID`, `PROMPT_VERSIONS`, `RUBRIC_VERSION`, `POLICY_VERSION` all surface on the methodology page. Responses are cached by `(prompt_hash, input_hash, model)` so re-runs cost nothing. I will consult the Anthropic API reference before writing `llm/anthropic.py` rather than working from memory.

**Policy (stage 5), fully deterministic:**
- `total = Σ dimension scores` (unknown dimensions contribute 0 but are reported as unscored; memos state "scored on N of 100 available points" so missing data reads as *unknown*, not as a judgment).
- `confidence = 0.45·dimension_coverage + 0.20·min(sources/4,1) + 0.20·site_fetch_success + 0.15·freshness` → `high ≥0.70`, `medium 0.45–0.69`, `low <0.45`.
- Band: `≥80 take_a_meeting`, `65–79 watch`, `≤64 pass`.
- Caps: `confidence == low` **or** company website never successfully fetched ⇒ capped at `watch`, with `cap_reason` recorded and shown in the UI.

## 6. UI structure

Static, self-contained, no build step. Cards are **server-rendered by Jinja2**; JavaScript only toggles `hidden` and reorders existing nodes via `data-*` attributes — so search/filter/sort work without ever constructing markup, and the page degrades gracefully with JS off.

- **`index.html`** — query + thesis summary, candidate totals, recommendation counts, text search, recommendation filter, confidence filter, sort by score, one card per company (name, one-liner, score, recommendation badge, confidence badge, link to report), and an explicit "excluded / failed" section.
- **`companies/<id>.html`** — recommendation + score + confidence above the fold; "Why this call" (band, cap reason, top contributing dimensions); plain-language product explanation; score breakdown table with per-dimension rationale, evidence links and `unknown` markers; team / product / market analysis; risks; open questions; what would change the recommendation; `<details>` blocks of supporting evidence, each tagged **Company claim** / **Third-party** / **Inference**, with clickable source citations.
- **`methodology.html`** — thesis (rendered from `config/thesis.md`), rubric weights, thresholds, confidence policy, data sources, evidence rules, model + prompt versions with hashes, limitations.

## 7. Error-handling strategy

Per-company isolation is the core rule: every company runs inside a boundary that records `CompanyFailure{company_id, stage, error_type, sanitized_message}` and lets the run continue. A failed company appears in the manifest, in `ranking.md`, and in the site's excluded section — it is never silently dropped.

Fatal (abort run): unreadable/missing required input artifact, unwritable run directory, zero candidates found, missing API key when a live provider is selected. Everything else degrades. Exit codes: `0` clean, `1` fatal, `2` partial success (`--strict` promotes 2 to 1). Sanitized messages only — no absolute paths, no headers, no key fragments.

## 8. Security controls

- **Secrets** — API key from env only, never a CLI flag, never written to any artifact; `Authorization`/`x-api-key` redacted in every logged request; a test greps the whole generated site and all artifacts for key-shaped patterns and for absolute filesystem paths.
- **Fetching** — http/https only; DNS-resolved IP rejected if loopback/private/link-local/reserved; non-standard ports blocked; redirects capped at 3 and each hop re-validated; response size and content-type capped; robots.txt honored.
- **Filesystem** — slug regex on every ID; all path construction through `RunStore` with a containment assertion; no user-controlled path segments.
- **UI** — Jinja2 `autoescape` on for HTML; citation URLs validated as http/https before becoming anchors (unsafe ones render as inert text); `rel="noopener noreferrer"`; **zero `innerHTML`** (asserted by a test that greps `app.js`); embedded JSON escaped for `</`; restrictive CSP meta (`default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:`); no analytics, no CDN, no external fonts.
- **Prompt injection** — treated as a residual risk, mitigated by schema-locked output + citation validation, documented honestly in `LIMITATIONS.md`.

## 9. Testing strategy

Tests must pass with **no network and no API key** — enforced by an autouse fixture that swaps in an `httpx.MockTransport` raising on any unregistered host and strips `*_API_KEY` from the environment.

- **Unit** — slug stability; HN JSON → candidates (fixtures); domain dedupe and blocklist; URL safety and private-IP rejection; trafilatura extraction on saved HTML; evidence validator (unknown source ID rejected, quote-not-in-source rejected); analysis validator (unknown evidence ID, model-supplied `recommendation`, total mismatch); market-number scrubber; confidence component table; retry-once semantics (asserts the provider is called at most twice); failure isolation (one poisoned company, run still completes).
- **Policy tables** — boundaries 64/65/79/80, low-confidence cap, no-website cap, all-unknown dimensions.
- **Golden** — memo Markdown and company HTML for one fixture company, byte-compared against committed goldens (`--snapshot-update` to refresh).
- **Security** — XSS payload in a company name and in an evidence quote appears escaped, not live; `javascript:` citation is not an anchor; no `innerHTML`; no secrets/paths in output.
- **E2E offline** — `demo` produces every required artifact path, correct manifest stage statuses, and a site whose ranking order matches the scores.
- **Determinism** — two demo runs with an injected fixed clock produce byte-identical output.

Gate: `ruff check`, `ruff format --check`, `mypy src` (strict on `src`, `ignore_missing_imports` for bs4/trafilatura/jinja2 as needed), `pytest`. Coverage target ~80% on `stages/`, `llm/validate.py`, `policy.py`, `util/`.

## 10. Implementation order

Each stage ends with the required report and waits for the user's manual commit. Estimates are honest, not padded.

| # | Stage | Deliverable | Est. |
|---|---|---|---|
| S0 | Scaffold | pyproject/uv, lint+type+test config, CI, all 8 commands stubbed with working `--help`, doc stubs with `TODO(author)`, `worklog/0001` | 30 m |
| S1 | Models + store | Pydantic models, `RunStore` atomic/contained IO, manifest writer, unit tests | 45 m |
| S2 | Source | Algolia client, fixtures, filtering/dedupe/ranking, `candidates.json`, tests | 45 m |
| S3 | Enrich | hardened HTTP client, SSRF guard, robots, extraction, `raw/` + `extracted/`, tests | 60 m |
| S4 | LLM boundary | provider protocol, Anthropic adapter, FakeProvider, prompt files + hashing, redacted call log, retry-once driver, tests | 50 m |
| S5 | Extract evidence | prompt v1, evidence schema + citation/quote validators, `evidence/`, tests | 45 m |
| S6 | Analyze | prompt v1, rubric scoring, evidence-citation + no-recommendation + market-number validators, `analyses/`, tests | 60 m |
| S7 | Policy | confidence + banding + caps, pure functions, table tests | 30 m |
| S8 | Render | memo and ranking templates, golden tests | 40 m |
| S9 | Site | templates, CSS, vanilla JS filters, `serve`, security tests | 75 m |
| S10 | Orchestrate | `run` + `demo`, manifest completeness, e2e offline test, committed demo run | 45 m |
| S11 | Docs | DECISIONS / AI_WORKFLOW / EVALUATION / LIMITATIONS filled with verifiable fact + `TODO(author)` markers, README quickstart, sanitized LLM samples | 40 m |
| S12 | Live smoke | one real run against the real API and real websites — **requires the user's key and explicit go-ahead** | 30 m |

Total ≈ **9 h 45 m**, i.e. over the eight-hour budget. Cut order if time runs short, worst-value first: S12 live run → site filters beyond search → extra unit tests → docs prose (keep the facts, drop the polish). **Never cut:** deterministic policy, citation validators, offline tests, output escaping.

## 11. Explicit non-goals

These are deliberate exclusions, not oversights. Each was considered and dropped to protect the
eight-hour budget or because the assignment forbids it.

React, Next.js, FastAPI, Flask, any database, LangGraph, vector DB, embeddings, background workers, queues, auth, cloud deployment, browser automation, multi-agent orchestration. Also out of scope: funding/Crunchbase data, founder-identity resolution, crawling beyond ~4 pages per company, cross-run deduplication, incremental re-crawl, PDF export, email digests, CRM integration, model fine-tuning, LLM-driven ranking, and any calibration of the rubric against real investment outcomes (there is no ground truth here, and claiming otherwise would be dishonest).

## 12. Risks to completing within eight hours

1. **Estimate exceeds budget** (~9.75 h vs 8 h) — highest-probability risk. Mitigation: the documented cut order above; S9 and S6 are the two most compressible stages.
2. **HN yields weak candidates for a niche query** — many stories are articles, not companies. Mitigation: query variants, domain blocklist, widened date window, `--seed-file` escape hatch, and honest shortfall reporting instead of padding.
3. **Sparse website content** — JS-only sites and bot walls, with browser automation banned. Mitigation: sparse evidence becomes `unknown` + confidence penalty + a Watch cap, which is the designed-for behaviour; documented in `LIMITATIONS.md`.
4. **LLM citation discipline** — the model inventing evidence IDs or market numbers. Mitigation: schema-locked output, one repair retry, hard validators that strip rather than trust. FakeProvider means tests are never blocked on model behaviour.
5. **Cost/latency/rate limits** — ~30 live calls per run. Mitigation: concurrency cap 3, content-hash cache, `--limit` default 15.
6. **UI scope creep** — mitigated by one CSS file, server-rendered cards, and JS restricted to show/hide/reorder.
7. **mypy vs untyped third-party libs** — timeboxed via per-module `ignore_missing_imports`; not worth burning budget on stubs.
8. **Worklog honesty** — reconstructing a worklog at the end invites fabrication, so entries are written at each stage boundary from what actually happened, with `TODO(author)` wherever first-person judgment belongs.
9. **Review-turnaround coupling** — since I never commit, stage cadence depends on the user's review pace; batching reviews is fine for progress but coarsens the Git history the assignment asks to see.

---

## Verification

- `uv run pytest` — full suite, offline, no key (also enforced in CI).
- `uv run ruff check . && uv run ruff format --check . && uv run mypy src` — clean.
- `uv run vc-scout demo` — regenerates the committed fixture run; `git status` should show no diff (determinism check).
- `uv run vc-scout serve --run-id demo` → open `http://127.0.0.1:8765`: dashboard filters/sort/search work; a company page shows recommendation above the fold, labelled evidence, and working source links; methodology page shows live rubric/threshold/prompt-version values.
- Artifact replay: delete `site/`, run `uv run vc-scout build-site --run-id demo`, confirm identical output from persisted inputs alone.
- Security sweep: `grep -rn "innerHTML" src/vc_scout/templates/site/assets/app.js` returns nothing; XSS and `javascript:` fixture tests pass.
- Live path (S12, only with the user's key and go-ahead): `uv run vc-scout run --query "AI agents for SMB operations" --limit 15 --run-id ai-agents-smb-demo`.

---

## Author's notes

TODO(author): assessment of whether the deterministic-policy / LLM-boundary split was the right
call once you had seen real model output.

TODO(author): what surprised you about the quality of Hacker News as a discovery source.

TODO(author): what you would build next if this went past a take-home.

*(These are intentionally unwritten. Generated prose does not belong in a first-person section —
see `docs/AI_WORKFLOW.md` for how AI assistance was actually used on this repository.)*
