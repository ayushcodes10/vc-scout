"""The static site: what it says, what it links to, and what it refuses to execute.

The site is the only artifact a partner is likely to read, and the only one rendered from
text that came off third-party pages into a format that can execute. Both facts are what
these tests are about: the pages must agree with the artifacts, and nothing in them may
run.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from pathlib import Path

import pytest

from tests.unit.analysis_fixtures import analysis, dossier
from tests.unit.memo_fixtures import (
    LOW,
    bundles,
    meeting_analysis,
    mismatch_analysis,
    seed_rendered_run,
    thin_analysis,
)
from vc_scout.models.enums import AssessmentStatus, Recommendation, ThesisFit
from vc_scout.render.html import SITE_TEMPLATE_VERSION, embed_json, internal_href, safe_href
from vc_scout.render.ranking import sort_key
from vc_scout.rubric import RUBRIC
from vc_scout.stages.recommend import run_recommend
from vc_scout.stages.ui import MissingArtifactError, run_build_ui
from vc_scout.store import RunStore

HREF = re.compile(r'href="([^"]+)"')
SRC = re.compile(r'src="([^"]+)"')


@pytest.fixture
def store(tmp_path: Path) -> RunStore:
    return RunStore("source-test", runs_root=tmp_path)


def seed(store: RunStore, specs: list[tuple[object, object]]) -> None:
    """Seed the run and render the Markdown stage the site builds on."""
    seed_rendered_run(store, specs)  # type: ignore[arg-type]
    run_recommend(store=store)


def build(store: RunStore, count: int = 15, **_: object) -> object:
    seeds = bundles(count)
    seed(store, [(bundle, thin_analysis(bundle)) for bundle in seeds])
    return run_build_ui(store=store)


def page(store: RunStore, company_id: str) -> str:
    return (store.site_dir / "companies" / f"{company_id}.html").read_text()


def index(store: RunStore) -> str:
    return (store.site_dir / "index.html").read_text()


def zero_claim(company_id: str):  # type: ignore[no-untyped-def]
    bundle = dossier(company_id=company_id, claims=0, unknowns=3)
    startup = analysis(
        bundle,
        total=14,
        status=AssessmentStatus.NOT_ASSESSABLE,
        confidence=LOW,
        thesis_verdict=ThesisFit.UNDETERMINED,
        thesis_evidence=False,
        suggested=Recommendation.PASS,
    )
    return bundle, startup


# -- generation --------------------------------------------------------------


def test_a_full_run_produces_one_page_per_company_plus_the_index(store: RunStore) -> None:
    outcome = build(store, 15)

    assert outcome.report.pages_written == 16  # type: ignore[attr-defined]
    assert len(outcome.report.company_pages) == 15  # type: ignore[attr-defined]
    assert (store.site_dir / "index.html").is_file()
    assert (store.site_dir / "assets" / "styles.css").is_file()
    assert (store.site_dir / "assets" / "app.js").is_file()
    assert (store.site_dir / "ui-report.json").is_file()
    assert len(list((store.site_dir / "companies").glob("*.html"))) == 15


def test_rebuilding_is_byte_identical(store: RunStore) -> None:
    build(store, 5)
    first = {
        path.relative_to(store.site_dir).as_posix(): path.read_bytes()
        for path in sorted(store.site_dir.rglob("*"))
        if path.is_file()
    }

    run_build_ui(store=store, force=True)
    second = {
        path.relative_to(store.site_dir).as_posix(): path.read_bytes()
        for path in sorted(store.site_dir.rglob("*"))
        if path.is_file()
    }
    assert second == first


def test_no_page_carries_a_build_timestamp(store: RunStore) -> None:
    build(store, 3)
    stamp = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}")
    assert not stamp.search(index(store))
    assert not stamp.search((store.site_dir / "ui-report.json").read_text())


def test_the_report_reconciles_with_the_recommendation_report(store: RunStore) -> None:
    seeds = bundles(4)
    seed(
        store,
        [
            (seeds[0], meeting_analysis(seeds[0])),
            (seeds[1], mismatch_analysis(seeds[1])),
            (seeds[2], thin_analysis(seeds[2])),
            (seeds[3], thin_analysis(seeds[3])),
        ],
    )
    outcome = run_build_ui(store=store)
    recommendation_report = store.read_recommendation_report()

    assert outcome.report.template_version == SITE_TEMPLATE_VERSION
    assert outcome.report.candidate_count == recommendation_report.candidate_count
    assert sum(outcome.report.recommendations.values()) == recommendation_report.memos_written
    assert outcome.report.recommendations["take-a-meeting"] == 1
    assert outcome.report.recommendations == {"pass": 1, "take-a-meeting": 1, "watch": 2}
    assert outcome.report.confidence_counts == recommendation_report.confidence_counts
    assert outcome.report.component_status_counts == (recommendation_report.component_status_counts)
    assert outcome.report.sources_cited == recommendation_report.referenced_sources


def test_the_index_counts_match_the_pages_it_links_to(store: RunStore) -> None:
    seeds = bundles(4)
    seed(
        store,
        [
            (seeds[0], meeting_analysis(seeds[0])),
            (seeds[1], mismatch_analysis(seeds[1])),
            (seeds[2], thin_analysis(seeds[2])),
            (seeds[3], thin_analysis(seeds[3])),
        ],
    )
    run_build_ui(store=store)
    text = index(store)

    badges = re.findall(r'badge badge--([a-z-]+)">([^<]+)<', text)
    counted = {slug: badges.count((slug, label)) for slug, label in set(badges)}
    assert counted == {"take-a-meeting": 1, "pass": 1, "watch": 2}
    assert ">Take a meeting</span>" in text


def test_page_order_matches_the_markdown_ranking(store: RunStore) -> None:
    seeds = bundles(6)
    seed(
        store,
        [
            (seeds[0], meeting_analysis(seeds[0])),
            (seeds[1], mismatch_analysis(seeds[1], total=40)),
            (seeds[2], thin_analysis(seeds[2])),
            (seeds[3], mismatch_analysis(seeds[3], total=20)),
            (seeds[4], zero_claim("co-04")[1]),
            (seeds[5], thin_analysis(seeds[5])),
        ],
    )
    run_build_ui(store=store)

    expected = store.read_recommendation_report().ordered_company_ids
    rendered = re.findall(r'<tr data-company="([^"]+)"', index(store))
    assert rendered == expected

    # The workflow position on each page agrees with its position in that order.
    for position, company_id in enumerate(expected, start=1):
        assert f"Workflow position {position}" in page(store, company_id)


# -- links -------------------------------------------------------------------


def test_every_internal_link_resolves_on_disk(store: RunStore) -> None:
    build(store, 15)
    checked = 0
    for path in [store.site_dir / "index.html", *(store.site_dir / "companies").glob("*.html")]:
        text = path.read_text()
        for href in HREF.findall(text) + SRC.findall(text):
            if href.startswith(("http://", "https://", "#")):
                continue
            assert (path.parent / href).resolve().exists(), f"{path.name} -> {href}"
            checked += 1
    assert checked > 15


def test_every_company_page_links_back_to_the_portfolio_its_memo_and_its_neighbours(
    store: RunStore,
) -> None:
    build(store, 4)
    order = store.read_recommendation_report().ordered_company_ids

    for position, company_id in enumerate(order):
        text = page(store, company_id)
        assert 'href="../index.html"' in text
        assert f'href="../../memos/{company_id}.md"' in text
        if position > 0:
            assert f'href="{order[position - 1]}.html" rel="prev"' in text
        else:
            assert 'rel="prev"' not in text
        if position + 1 < len(order):
            assert f'href="{order[position + 1]}.html" rel="next"' in text
        else:
            assert 'rel="next"' not in text


def test_an_external_link_is_marked_and_carries_no_referrer(store: RunStore) -> None:
    build(store, 2)
    text = page(store, store.read_recommendation_report().ordered_company_ids[0])
    assert 'rel="noopener noreferrer nofollow"' in text
    assert 'class="external"' in text
    assert '<meta name="referrer" content="no-referrer">' in text


# -- content -----------------------------------------------------------------


def test_a_company_page_renders_all_seven_dimensions_and_a_total(store: RunStore) -> None:
    seeds = bundles(1)
    seed(store, [(seeds[0], mismatch_analysis(seeds[0]))])
    run_build_ui(store=store)
    text = page(store, "co-00")

    for spec in RUBRIC:
        assert text.count(f'<th scope="row" data-label="Dimension">{spec.title}</th>') == 1
    assert '<th scope="row" data-label="Dimension">Total</th>' in text
    stored, _ = store.read_analysis("co-00")
    assert f'{stored.total_score}<span class="muted">/100</span>' in text


def test_exactly_the_validated_changers_render(store: RunStore) -> None:
    seeds = bundles(1)
    startup = analysis(seeds[0], total=30, status=AssessmentStatus.PARTIALLY_SUPPORTED, changers=3)
    seed(store, [(seeds[0], startup)])
    run_build_ui(store=store)
    text = page(store, "co-00")

    section = text.split('id="changers-heading"')[1].split("</section>")[0]
    assert section.count("<li>") == len(startup.recommendation_changers)
    assert 2 <= section.count("<li>") <= 3
    for changer in startup.recommendation_changers:
        assert changer in section


def test_guardrails_and_a_model_policy_disagreement_are_visible(store: RunStore) -> None:
    bundle, startup = zero_claim("co-00")
    seed(store, [(bundle, startup)])
    run_build_ui(store=store)
    text = page(store, "co-00")

    assert "<strong>Guardrail applied.</strong>" in text
    assert "No evidence claim could be extracted at all" in text
    assert "The analysis model suggested pass; the deterministic policy decided watch." in text
    assert "The policy is binding." in text
    # The raw policy identifier is not what a partner should have to read.
    assert "zero_claim_dossier" not in text

    listing = index(store)
    # The table says what the guardrail was, not merely that there was one.
    assert "No usable evidence" in listing
    assert ">Guardrail<" not in listing
    assert "Policy override" in listing
    assert "Model differs" not in listing


def test_identity_and_analysis_warnings_are_labelled_separately(store: RunStore) -> None:
    """An identity warning is a different problem from an analysis note, and says so."""
    seeds = bundles(1)
    startup = analysis(
        seeds[0],
        total=30,
        status=AssessmentStatus.PARTIALLY_SUPPORTED,
        identity_warnings=("The sources may describe memorilabs.ai, not this candidate.",),
    ).model_copy(update={"analysis_warnings": ["The dossier is thin on traction."]})
    seed(store, [(seeds[0], startup)])
    run_build_ui(store=store)
    text = page(store, "co-00")

    assert "<h3>Identity warnings</h3>" in text
    assert "may describe memorilabs.ai" in text
    assert "<h3>Analysis warnings</h3>" in text
    assert "thin on traction" in text


def test_a_zero_claim_watch_reads_as_missing_evidence_not_weakness(store: RunStore) -> None:
    bundle, startup = zero_claim("co-00")
    seed(store, [(bundle, startup)])
    run_build_ui(store=store)
    text = page(store, "co-00")

    assert "Watch on insufficient evidence, not on merit" in text
    assert "it is not evidence of weakness" in text
    assert "These are gaps in the research. None of them is a finding about the company." in text
    assert "Not established by the sources" in text


def test_the_no_meeting_explanation_uses_the_runs_own_counts(store: RunStore) -> None:
    build(store, 3)
    text = index(store)
    report = json.loads((store.site_dir / "ui-report.json").read_text())
    statuses = report["component_status_counts"]

    assert "Why no company was recommended for a meeting" in text
    assert "No candidate reached the take-a-meeting band at 80/100." in text
    assert f"{statuses.get('supported', 0)} supported" in text
    assert f"the {sum(statuses.values())} scored dimension slots" in text
    assert "No score was raised to produce a recommendation." in text


def test_the_no_meeting_panel_is_absent_when_a_meeting_was_recommended(store: RunStore) -> None:
    seeds = bundles(2)
    seed(
        store,
        [(seeds[0], meeting_analysis(seeds[0])), (seeds[1], thin_analysis(seeds[1]))],
    )
    run_build_ui(store=store)
    assert "Why no company was recommended for a meeting" not in index(store)


def test_internal_evidence_identifiers_stay_out_of_the_reading_experience(
    store: RunStore,
) -> None:
    seeds = bundles(1)
    seed(store, [(seeds[0], mismatch_analysis(seeds[0]))])
    run_build_ui(store=store)
    text = page(store, "co-00")

    assert not re.search(r"\bev-[0-9a-f]{12}\b", text)
    assert not re.search(r"\bunk-[0-9a-f]{12}\b", text)
    assert "[S1]" in text


# -- triage hierarchy and density --------------------------------------------


def test_the_pipeline_precedes_the_reference_material(store: RunStore) -> None:
    """A partner opens this to triage. The thesis is reference, and sits below."""
    build(store, 3)
    text = index(store)

    summary = text.index("Run summary")
    controls = text.index('id="vcs-controls"')
    table = text.index('id="vcs-table"')
    thesis = text.index("Investment thesis")
    no_meeting = text.index("Why no company was recommended for a meeting")
    assert summary < controls < table < thesis < no_meeting


def test_both_reference_panels_are_native_disclosures_collapsed_by_default(
    store: RunStore,
) -> None:
    build(store, 3)
    text = index(store)

    assert text.count('<details class="disclosure">') == 2
    # Collapsed: no `open` attribute anywhere. Native <details> needs no JavaScript and is
    # keyboard accessible without a role, a tabindex or a key handler.
    assert '<details class="disclosure" open>' not in text
    assert " open>" not in text
    assert text.count("<summary>") == 2
    assert 'role="button"' not in text
    assert "aria-expanded" not in text


def test_the_summary_strip_labels_each_confidence_level(store: RunStore) -> None:
    seeds = bundles(3)
    seed(
        store,
        [
            (seeds[0], meeting_analysis(seeds[0])),
            (seeds[1], mismatch_analysis(seeds[1])),
            (seeds[2], thin_analysis(seeds[2])),
        ],
    )
    run_build_ui(store=store)
    text = index(store)

    # "1 / 10 / 4" with a legend underneath made a reader decode before they could read.
    assert "high / medium / low" not in text
    for label in ("High", "Medium", "Low"):
        assert f'summary__label">{label}</p>' in text
    assert '<p class="summary__label">Candidates</p>' in text
    assert '<p class="summary__label">Sources cited</p>' in text


def test_the_ranking_link_is_named_for_what_it_does(store: RunStore) -> None:
    build(store, 2)
    text = index(store)
    assert ">Export ranking</a>" in text
    assert "Ranking (Markdown)" not in text


def test_long_prose_is_clamped_in_the_table(store: RunStore) -> None:
    """The portfolio is a scan. A memo-length paragraph in a row defeats it."""
    build(store, 3)
    text = index(store)

    assert text.count('class="clamp-2"') == 3
    assert text.count('class="cell-note clamp-1"') == 6  # buyer and workflow per row
    css = (store.site_dir / "assets" / "styles.css").read_text()
    assert "-webkit-line-clamp: 2" in css
    # A browser without line clamping still gets a bounded cell rather than a paragraph.
    assert ".clamp-2 { max-height:" in css


def test_each_row_offers_a_named_analysis_link_beside_the_company_name(
    store: RunStore,
) -> None:
    build(store, 3)
    text = index(store)

    assert text.count(">View analysis</a>") == 3
    for company_id in ("co-00", "co-01", "co-02"):
        assert text.count(f'href="companies/{company_id}.html"') == 2
    # The row is not a click target: that would break table semantics and fight the
    # external website link sitting inside it.
    assert "data-href" not in text
    css = (store.site_dir / "assets" / "styles.css").read_text()
    assert "tbody tr { cursor: pointer" not in css
    script = (store.site_dir / "assets" / "app.js").read_text()
    assert "tbody tr" not in script or 'addEventListener("click"' not in script


def test_the_reset_control_is_a_labelled_keyboard_accessible_button(
    store: RunStore,
) -> None:
    build(store, 2)
    text = index(store)

    assert '<button type="button" id="vcs-reset" class="button--secondary">Reset</button>' in text
    # A real button is focusable and activates on Enter and Space without any handler of
    # ours; a div with a click listener is neither.
    assert '<div id="vcs-reset"' not in text
    script = (store.site_dir / "assets" / "app.js").read_text()
    assert 'getElementById("vcs-reset")' in script


# -- recommendation clarity --------------------------------------------------


def test_a_watch_says_which_kind_of_watch_it_is(store: RunStore) -> None:
    """Every watch in this run is an evidence shortfall, and a bare badge hides that."""
    zero = zero_claim("co-00")
    thin_bundle = bundles(2)[1]
    seed(store, [zero, (thin_bundle, thin_analysis(thin_bundle))])
    run_build_ui(store=store)
    text = index(store)

    assert "no usable evidence" in text
    assert "needs research" in text
    for company_id, qualifier in (("co-00", "no usable evidence"), ("co-01", "needs research")):
        row = text.split(f'data-company="{company_id}"')[1].split("</tr>")[0]
        assert ">Watch</span>" in row
        assert qualifier in row


def test_an_evidence_backed_thesis_mismatch_pass_is_marked_as_one(store: RunStore) -> None:
    seeds = bundles(2)
    startup = analysis(seeds[1], total=30, status=AssessmentStatus.PARTIALLY_SUPPORTED, buyer=None)
    seed(store, [(seeds[0], mismatch_analysis(seeds[0])), (seeds[1], startup)])
    run_build_ui(store=store)
    text = index(store)

    mismatch_row = text.split('data-company="co-00"')[1].split("</tr>")[0]
    ordinary_row = text.split('data-company="co-01"')[1].split("</tr>")[0]
    assert ">Pass</span>" in mismatch_row
    assert "outside thesis" in mismatch_row
    assert ">Pass</span>" in ordinary_row
    assert "outside thesis" not in ordinary_row


def test_the_displayed_label_never_changes_the_persisted_decision(store: RunStore) -> None:
    zero = zero_claim("co-00")
    seed(store, [zero])
    run_build_ui(store=store)

    _, recommendation = store.read_analysis("co-00")
    assert recommendation.decision is Recommendation.WATCH
    assert recommendation.guardrails_applied == ["zero_claim_dossier"]
    stored = store.read_recommendation_report()
    assert stored.recommendations == {"watch": 1}


# -- safety ------------------------------------------------------------------


HOSTILE_NAME = '<script>alert(1)</script><img src=x onerror=alert(2)>"><b>Meeting</b>'


def test_hostile_company_text_is_escaped_not_executed(store: RunStore) -> None:
    seeds = bundles(1)
    seed(store, [(seeds[0], mismatch_analysis(seeds[0]))])
    candidate_set = store.read_candidates()
    hostile = candidate_set.candidates[0].model_copy(update={"name": HOSTILE_NAME})
    store.write_candidates(candidate_set.model_copy(update={"candidates": [hostile]}))
    run_build_ui(store=store, force=True)

    for text in (index(store), page(store, "co-00")):
        assert "<script>alert(1)</script>" not in text
        assert "&lt;script&gt;" in text
        assert "<img" not in text
        # The payload survives as visible text; what must not survive is a live attribute.
        assert not re.search(r"<[^>]*\son[a-z]+=", text)
        assert "onerror=alert(2)&gt;" in text


def test_a_script_closing_payload_cannot_break_the_embedded_data(store: RunStore) -> None:
    seeds = bundles(1)
    seed(store, [(seeds[0], mismatch_analysis(seeds[0]))])
    candidate_set = store.read_candidates()
    payload = "</script><script>alert(1)</script>"
    hostile = candidate_set.candidates[0].model_copy(update={"name": payload})
    store.write_candidates(candidate_set.model_copy(update={"candidates": [hostile]}))
    run_build_ui(store=store, force=True)
    text = index(store)

    block = text.split('id="vcs-filter-data">')[1].split("</script>")[0]
    assert "<" not in block
    assert "\\u003c" in block
    # The block still parses as the data the page needs.
    assert json.loads(block)[0]["id"] == "co-00"
    # Exactly one script element carries the data, and one loads the app.
    assert text.count("<script") == 2


def test_embed_json_escapes_every_element_terminating_character() -> None:
    escaped = str(embed_json({"x": "</script><b>&</b>  "}))
    for char in ("<", ">", "&", " ", " "):
        assert char not in escaped
    assert json.loads(escaped)["x"] == "</script><b>&</b>  "


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "data:text/html;base64,PHNjcmlwdD4=",
        "file:///etc/passwd",
        "vbscript:msgbox(1)",
        "//evil.example/x",
        "https://example.com/a b",
    ],
)
def test_only_absolute_http_urls_become_links(url: str) -> None:
    assert safe_href(url) is None


def test_an_internal_link_target_is_validated() -> None:
    assert internal_href("companies/acme.html") == "companies/acme.html"
    assert internal_href("../../memos/acme.md") == "../../memos/acme.md"
    # Two levels reach the run root, which is as far as any generated page needs to go.
    for unusable in (
        "../../../escape.html",
        "javascript:alert(1)",
        "a b.html",
        "/absolute.html",
        "https://evil.example/x",
        "companies/../../../x.html",
    ):
        with pytest.raises(ValueError, match="refusing to link"):
            internal_href(unusable)


def test_no_page_reaches_an_external_origin_for_an_asset(store: RunStore) -> None:
    build(store, 3)
    for path in [store.site_dir / "index.html", *(store.site_dir / "companies").glob("*.html")]:
        text = path.read_text()
        assert "<img" not in text
        assert "@import" not in text
        for tag in re.findall(r"<(?:link|script)[^>]*>", text):
            remote = re.search(r'(?:href|src)="(https?:)?//', tag)
            assert remote is None, tag
        assert "//fonts." not in text


def test_every_page_declares_a_restrictive_content_security_policy(store: RunStore) -> None:
    build(store, 3)
    for path in [store.site_dir / "index.html", *(store.site_dir / "companies").glob("*.html")]:
        text = path.read_text()
        csp = re.search(r'Content-Security-Policy" content="([^"]+)"', text)
        assert csp is not None
        policy = csp.group(1)
        assert "default-src 'none'" in policy
        assert "script-src 'self'" in policy
        assert "unsafe-inline" not in policy
        assert "unsafe-eval" not in policy


def test_the_script_never_assigns_markup_and_never_calls_out(store: RunStore) -> None:
    build(store, 2)
    script = (store.site_dir / "assets" / "app.js").read_text()
    for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval("):
        assert forbidden not in script
    for forbidden in ("fetch(", "XMLHttpRequest", "WebSocket", "import("):
        assert forbidden not in script
    assert "textContent" in script


def test_no_inline_event_handler_or_inline_style_is_emitted(store: RunStore) -> None:
    build(store, 3)
    for path in [store.site_dir / "index.html", *(store.site_dir / "companies").glob("*.html")]:
        text = path.read_text()
        assert not re.search(r"\son[a-z]+=", text)
        assert not re.search(r"<[^>]+\sstyle=", text)
        assert "javascript:" not in text


# -- accessibility -----------------------------------------------------------


class _Headings(HTMLParser):
    """Collects heading levels and form-control identifiers."""

    def __init__(self) -> None:
        super().__init__()
        self.levels: list[int] = []
        self.labels: list[str] = []
        self.controls: list[str] = []
        self.buttons: list[str] = []
        self.tables = 0
        self.headers = 0

    def __init_extra__(self) -> None:  # pragma: no cover - documentation only
        """``buttons`` are tracked apart: their accessible name is their own text."""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag in {"h1", "h2", "h3", "h4"}:
            self.levels.append(int(tag[1]))
        if tag == "label" and values.get("for"):
            self.labels.append(str(values["for"]))
        if tag in {"input", "select"} and values.get("id"):
            self.controls.append(str(values["id"]))
        if tag == "button":
            self.buttons.append(str(values.get("id") or ""))
        if tag == "table":
            self.tables += 1
        if tag == "th" and values.get("scope"):
            self.headers += 1


def parsed(text: str) -> _Headings:
    parser = _Headings()
    parser.feed(text)
    return parser


def test_the_heading_hierarchy_starts_at_one_and_never_skips(store: RunStore) -> None:
    build(store, 3)
    for text in (index(store), page(store, "co-00")):
        levels = parsed(text).levels
        assert levels[0] == 1
        assert levels.count(1) == 1
        for previous, current in zip(levels, levels[1:], strict=False):
            assert current <= previous + 1


def test_every_control_has_a_label_and_every_table_has_scoped_headers(
    store: RunStore,
) -> None:
    build(store, 3)
    parser = parsed(index(store))
    assert set(parser.controls) == set(parser.labels)
    assert parser.controls
    assert parser.tables == 1
    assert parser.headers >= 8

    detail = parsed(page(store, "co-00"))
    assert detail.tables == 1
    assert detail.headers >= 5


def test_the_call_is_never_carried_by_colour_alone(store: RunStore) -> None:
    build(store, 2)
    text = index(store)
    # Every badge states its call in words, and the stylesheet gives each a distinct glyph.
    for slug, label in (("watch", "Watch"), ("pass", "Pass")):
        if f"badge--{slug}" in text:
            assert f'badge badge--{slug}">{label}<' in text
    css = (store.site_dir / "assets" / "styles.css").read_text()
    for slug in ("take-a-meeting", "watch", "pass"):
        assert f".badge--{slug}::before" in css
    assert "prefers-reduced-motion" in css
    assert "@media print" in css


def test_the_stylesheet_has_no_remote_dependency(store: RunStore) -> None:
    build(store, 2)
    css = (store.site_dir / "assets" / "styles.css").read_text()
    assert "@import" not in css
    assert "url(" not in css
    assert "http://" not in css
    assert "https://" not in css
    assert "//fonts." not in css


# -- lifecycle ---------------------------------------------------------------


def test_a_stale_company_page_is_removed(store: RunStore) -> None:
    seeds = bundles(3)
    seed(store, [(bundle, mismatch_analysis(bundle)) for bundle in seeds])
    run_build_ui(store=store)
    assert (store.site_dir / "companies" / "co-01.html").is_file()

    store.delete_analysis("co-01")
    outcome = run_build_ui(store=store, force=True)

    assert not (store.site_dir / "companies" / "co-01.html").exists()
    assert [f.company_id for f in outcome.report.failures] == ["co-01"]
    assert (store.site_dir / "companies" / "co-00.html").is_file()
    assert "co-01 - no analysis was produced" in index(store)


def test_a_candidate_that_cannot_render_does_not_stop_the_build(store: RunStore) -> None:
    seeds = bundles(3)
    seed(store, [(bundle, mismatch_analysis(bundle)) for bundle in seeds])
    store.delete_evidence("co-02")
    outcome = run_build_ui(store=store, force=True)

    assert outcome.report.pages_written == 3  # two companies plus the index
    assert [f.company_id for f in outcome.report.failures] == ["co-02"]
    assert "Candidates without a company page" in index(store)


def test_force_replaces_only_what_the_generator_owns(store: RunStore) -> None:
    build(store, 2)
    keepsake = store.site_dir / "notes-from-the-partner.txt"
    keepsake.write_text("do not delete me")

    run_build_ui(store=store, force=True)
    assert keepsake.read_text() == "do not delete me"
    assert (store.site_dir / "index.html").is_file()


def test_the_stage_requires_the_markdown_stage_to_have_run(store: RunStore) -> None:
    seeds = bundles(1)
    seed_rendered_run(store, [(seeds[0], mismatch_analysis(seeds[0]))])
    with pytest.raises(MissingArtifactError, match="recommendation-report.json"):
        run_build_ui(store=store)


def test_the_stage_requires_candidates_and_an_analysis_report(store: RunStore) -> None:
    with pytest.raises(MissingArtifactError, match="candidates.json"):
        run_build_ui(store=store)


def test_building_needs_no_network_and_no_credential(store: RunStore) -> None:
    import socket

    with pytest.raises(RuntimeError, match="network access is disabled"):
        socket.create_connection(("example.com", 443))
    assert build(store, 2).report.pages_written == 3  # type: ignore[attr-defined]


def test_the_ranking_comparator_is_the_one_the_site_uses(store: RunStore) -> None:
    """Guards the shared ordering: the site must not grow its own sort."""
    seeds = bundles(4)
    seed(store, [(bundle, thin_analysis(bundle)) for bundle in seeds])
    run_build_ui(store=store)
    assert sort_key.__module__ == "vc_scout.render.ranking"
    assert re.findall(r'<tr data-company="([^"]+)"', index(store)) == (
        store.read_recommendation_report().ordered_company_ids
    )
