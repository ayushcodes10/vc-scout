# Worklog 01 - Scope and foundation

**Stage:** 1 of the order in `docs/PLAN.md` (foundation and domain contracts).
**Scope as given:** project skeleton, Typer CLI with placeholder commands, Pydantic
domain models, JSON artifact storage, scoring configuration, deterministic recommendation
policy, supporting files, initial unit tests. Explicitly excluded: any network call, any
LLM call.

## What was built

- `pyproject.toml` - uv project, `vc-scout` console script, Ruff / mypy(strict) / pytest
  configuration. Runtime dependencies are Typer and Pydantic only; nothing that touches
  the network is a dependency yet.
- `src/vc_scout/util/ids.py` - URL normalisation and content-derived, stable identifiers.
- `src/vc_scout/util/jsonio.py` - atomic, sorted-key JSON persistence.
- `src/vc_scout/rubric.py` - the seven scored dimensions and their weights, asserted at
  import to cover every dimension exactly once and total 100.
- `src/vc_scout/models/` - `SourceReference`, `TractionSignal`, `Candidate`,
  `ExtractedPage`, `EvidenceClaim`, `EvidenceDossier`, `ScoreComponent`,
  `StartupAnalysis`, `ResearchConfidence`, `RecommendationResult`, `RunManifest`, plus
  the `CandidateSet` / `PageBundle` artifact wrappers and shared enums.
- `src/vc_scout/policy.py` - `band_for`, `compute_confidence`, `decide`. No LLM, no
  network, no ambient clock.
- `src/vc_scout/store.py` - `RunStore`, the only module that constructs run paths.
- `src/vc_scout/cli.py` - eight required commands plus `config`, all with working
  `--help`. Pipeline commands are placeholders that name their implementing stage and
  exit `2`.
- `docs/PLAN.md`, `docs/DECISIONS.md`, `README.md`, `.env.example`, `.gitignore`.

## Contract decisions taken during implementation

Recorded in full in `docs/DECISIONS.md`; the two that changed the approved plan:

1. **Identifiers are content-derived rather than sequential** (D1). The stage brief
   required stable source and evidence IDs, which sequential numbering cannot provide
   across re-runs.
2. **The model's suggested recommendation is stored, not rejected** (D4). The approved
   plan had the analysis schema reject any model-supplied recommendation outright; the
   stage brief instead requires the suggestion to be kept separate from the deterministic
   call. The policy remains blind to it.

## Verification performed

Run at the end of the stage, on Python 3.12.13 with uv 0.12.5:

| Command | Result |
| --- | --- |
| `uv run pytest` | 118 passed |
| `uv run ruff check .` | All checks passed |
| `uv run ruff format --check .` | 33 files already formatted |
| `uv run mypy src` | no issues in 19 source files |
| `uv run vc-scout --help` | lists all eight required commands plus `config` |
| `uv run vc-scout config` | prints the rubric totalling 100 and the three bands |
| `uv run vc-scout source --query ... --run-id demo` | reports "not implemented yet", exits 2 |

Two tests failed on their first run. Both were incorrect expectations in the tests, not
defects in the code, and both were corrected by fixing the test:

1. `test_company_id_falls_back_to_domain_then_digest` expected `acme-ops` from the
   website fallback; the slug of `acme-ops.com` is `acme-ops-com`.
2. `test_source_reference_derives_id_and_domain` expected `normalize_url` to strip
   `www.`. It deliberately does not - rewriting a host would change a source's identity -
   and only the `domain` field collapses `www.` for grouping. The test now pins that
   distinction explicitly.

No network call and no LLM call is made by any code path in this stage; `tests/conftest.py`
enforces that by blocking sockets for the whole suite.

## Notes for the next stage

- `Settings.api_key_present` exists but nothing reads a credential yet.
- `compute_confidence` takes `source_count`, `website_fetched` and
  `newest_source_age_days` as explicit arguments. The enrich and analyze stages will need
  to supply them; they are not derivable from a `StartupAnalysis` alone.
- `tests/conftest.py` blocks sockets and strips `*_API_KEY` for the whole suite. Stage 3
  should add an `httpx.MockTransport` layer on top rather than relaxing this.

## Personal observations

TODO(author): your read on whether the domain model is carrying its weight, or whether
the validator density is over-engineering for a take-home.

TODO(author): anything about this stage that took longer than you expected, and why.
