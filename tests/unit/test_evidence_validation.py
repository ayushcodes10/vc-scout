"""Validation of model-supplied evidence.

The model is treated as an untrusted witness, and this is where that is enforced. Every
test here describes something a model plausibly does — cites a source it was never given,
paraphrases a quote, over-claims independent support — and pins the rejection.
"""

from __future__ import annotations

import pytest

from tests.unit.evidence_fixtures import HOMEPAGE_TEXT, NOW, claim_payload
from vc_scout.llm.validation import (
    EvidenceValidationError,
    SuppliedSource,
    closest_span,
    normalize_for_match,
    validate_evidence,
)
from vc_scout.models.enums import (
    EvidenceCategory,
    InferenceStatus,
    LlmErrorCategory,
    SourceKind,
    VerificationStatus,
)
from vc_scout.models.evidence import MAX_EXCERPT_CHARS
from vc_scout.models.source import SourceReference

HN_TEXT = "The thread has 42 points on Hacker News. The thread has 17 comments."
OTHER_TEXT = "An independent write-up says Acme Ops reconciles invoices for contractors."


def supplied() -> list[SuppliedSource]:
    home = SourceReference.create("https://acme-ops.example/", kind=SourceKind.COMPANY_PAGE)
    hn = SourceReference.create("https://news.ycombinator.com/item?id=1", kind=SourceKind.HN_STORY)
    return [
        SuppliedSource(reference=hn, text=HN_TEXT, role=None),
        SuppliedSource(reference=home, text=HOMEPAGE_TEXT, role="homepage"),
    ]


def ids() -> dict[str, str]:
    sources = supplied()
    return {"hn": sources[0].source_id, "home": sources[1].source_id}


def validate(payload: dict, *, sources: list[SuppliedSource] | None = None, website: bool = True):
    return validate_evidence(
        payload,
        company_id="acme-ops",
        sources=sources if sources is not None else supplied(),
        prompt_version="evidence_v1",
        provider="fake",
        model="fake-model-1",
        generated_at=NOW,
        website_available=website,
    )


def expect_rejection(payload: dict, **kw) -> EvidenceValidationError:
    with pytest.raises(EvidenceValidationError) as caught:
        validate(payload, **kw)
    return caught.value


# -- valid extraction --------------------------------------------------------


def test_a_well_formed_claim_becomes_a_dossier() -> None:
    outcome = validate({"claims": [claim_payload(ids()["home"])], "unknowns": [], "conflicts": []})
    dossier = outcome.dossier

    assert len(dossier.claims) == 1
    claim = dossier.claims[0]
    assert claim.category is EvidenceCategory.PRODUCT
    assert claim.verification_status is VerificationStatus.COMPANY_CLAIM
    assert claim.inference_status is InferenceStatus.EXPLICIT
    assert claim.source_ids == [ids()["home"]]
    assert dossier.prompt_version == "evidence_v1"
    assert dossier.provider == "fake"


def test_company_pages_are_labelled_as_company_claims() -> None:
    outcome = validate({"claims": [claim_payload(ids()["home"])], "unknowns": [], "conflicts": []})
    assert outcome.dossier.claims[0].verification_status is VerificationStatus.COMPANY_CLAIM


def test_hacker_news_metrics_are_labelled_as_community_signals() -> None:
    payload = {
        "claims": [
            claim_payload(
                ids()["hn"],
                excerpt="The thread has 42 points on Hacker News.",
                claim="The launch thread received 42 points on Hacker News.",
                category="traction",
                verification="community_signal",
            )
        ],
        "unknowns": [],
        "conflicts": [],
    }
    claim = validate(payload).dossier.claims[0]
    assert claim.verification_status is VerificationStatus.COMMUNITY_SIGNAL
    assert claim.category is EvidenceCategory.TRACTION


def test_unknowns_are_recorded_as_stated() -> None:
    payload = {
        "claims": [],
        "unknowns": [
            {
                "category": "team",
                "question": "Who founded the company?",
                "reason": "No team page was supplied.",
            }
        ],
        "conflicts": [],
    }
    dossier = validate(payload).dossier
    assert len(dossier.unknowns) == 1
    assert dossier.unknowns[0].category is EvidenceCategory.TEAM
    assert dossier.claims == []


def test_conflicting_sources_are_retained_rather_than_resolved() -> None:
    payload = {
        "claims": [],
        "unknowns": [],
        "conflicts": [
            {
                "category": "product",
                "summary": "The homepage and the thread describe different pricing.",
                "source_ids": [ids()["home"], ids()["hn"]],
                "excerpts": [
                    {"source_id": ids()["home"], "excerpt": "Plans start at 49 dollars per month"}
                ],
            }
        ],
    }
    dossier = validate(payload).dossier
    assert len(dossier.conflicts) == 1
    assert len(dossier.conflicts[0].source_ids) == 2


def test_explicit_and_inferred_claims_are_distinguished() -> None:
    payload = {
        "claims": [
            claim_payload(ids()["home"], inference="explicit"),
            claim_payload(
                ids()["home"],
                excerpt="Built by two engineers who ran a field service business",
                claim="The founders appear to have domain experience in field service.",
                category="team",
                inference="inferred",
            ),
        ],
        "unknowns": [],
        "conflicts": [],
    }
    statuses = [claim.inference_status for claim in validate(payload).dossier.claims]
    assert statuses == [InferenceStatus.EXPLICIT, InferenceStatus.INFERRED]


def test_missing_website_evidence_is_recorded_without_a_negative_claim() -> None:
    only_hn = [supplied()[0]]
    payload = {
        "claims": [],
        "unknowns": [{"category": "product", "question": "What does the product do?"}],
        "conflicts": [],
    }
    dossier = validate(payload, sources=only_hn, website=False).dossier

    assert dossier.source_coverage is not None
    assert dossier.source_coverage.website_available is False
    assert dossier.unknowns
    assert any("not the same as evidence of weakness" in w for w in dossier.warnings)


# -- rejections --------------------------------------------------------------


def test_an_unsupplied_source_id_is_rejected() -> None:
    error = expect_rejection(
        {"claims": [claim_payload("src-ffffffffffff")], "unknowns": [], "conflicts": []}
    )
    assert error.category is LlmErrorCategory.UNKNOWN_SOURCE_REFERENCE
    assert "was not supplied for this candidate" in error.errors[0]
    # The retry is told which IDs are valid.
    assert ids()["home"] in error.errors[0]


def test_a_claim_without_any_excerpt_is_rejected() -> None:
    payload = claim_payload(ids()["home"])
    payload["excerpts"] = []
    error = expect_rejection({"claims": [payload], "unknowns": [], "conflicts": []})
    assert "at least one supporting excerpt is required" in error.errors[0]


def test_an_empty_excerpt_is_rejected() -> None:
    payload = claim_payload(ids()["home"], excerpt="   ")
    error = expect_rejection({"claims": [payload], "unknowns": [], "conflicts": []})
    assert "empty" in error.errors[0]


def test_an_excerpt_absent_from_the_source_is_rejected() -> None:
    """The paraphrase case: plausible wording that the page never contained."""
    payload = claim_payload(
        ids()["home"], excerpt="serves thousands of enterprise customers worldwide"
    )
    error = expect_rejection({"claims": [payload], "unknowns": [], "conflicts": []})
    assert error.category is LlmErrorCategory.EXCERPT_NOT_FOUND
    assert "does not appear in the text of" in error.errors[0]


def test_an_excerpt_attached_to_the_wrong_source_is_rejected_and_named() -> None:
    payload = claim_payload(ids()["hn"], excerpt="reconciles invoices for plumbing contractors")
    error = expect_rejection({"claims": [payload], "unknowns": [], "conflicts": []})
    assert error.category is LlmErrorCategory.EXCERPT_NOT_FOUND
    assert f"It does appear in {ids()['home']}" in error.errors[0]


def test_an_excessively_long_excerpt_is_rejected() -> None:
    long_source = SuppliedSource(
        reference=SourceReference.create("https://long.example/", kind=SourceKind.COMPANY_PAGE),
        text="word " * 400,
        role="homepage",
    )
    payload = claim_payload(long_source.source_id, excerpt=("word " * 200).strip())
    error = expect_rejection(
        {"claims": [payload], "unknowns": [], "conflicts": []}, sources=[long_source]
    )
    assert f"longer than the {MAX_EXCERPT_CHARS}-character maximum" in error.errors[0]


def test_duplicate_claims_are_rejected() -> None:
    duplicate = claim_payload(ids()["home"])
    error = expect_rejection(
        {"claims": [duplicate, dict(duplicate)], "unknowns": [], "conflicts": []}
    )
    assert "duplicates an earlier claim" in " ".join(error.errors)


def test_independently_supported_with_one_source_is_rejected() -> None:
    payload = claim_payload(ids()["home"], verification="independently_supported")
    error = expect_rejection({"claims": [payload], "unknowns": [], "conflicts": []})
    assert "requires at least two separate sources" in error.errors[0]


def test_independently_supported_with_two_sources_is_accepted() -> None:
    other = SuppliedSource(
        reference=SourceReference.create("https://press.example/x", kind=SourceKind.OTHER),
        text=OTHER_TEXT,
        role=None,
    )
    payload = claim_payload(
        ids()["home"],
        verification="independently_supported",
        extra_excerpts=[
            {"source_id": other.source_id, "excerpt": "reconciles invoices for contractors"}
        ],
    )
    dossier = validate(
        {"claims": [payload], "unknowns": [], "conflicts": []}, sources=[*supplied(), other]
    ).dossier
    assert dossier.claims[0].verification_status is VerificationStatus.INDEPENDENTLY_SUPPORTED
    assert len(dossier.claims[0].source_ids) == 2


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("category", "vibes"),
        ("verification_status", "verified"),
        ("inference_status", "guessed"),
    ],
)
def test_invalid_vocabularies_are_rejected(field: str, value: str) -> None:
    payload = claim_payload(ids()["home"])
    payload[field] = value
    error = expect_rejection({"claims": [payload], "unknowns": [], "conflicts": []})
    assert field in error.errors[0]


def test_a_conflict_citing_one_source_is_rejected() -> None:
    payload = {
        "claims": [],
        "unknowns": [],
        "conflicts": [
            {"category": "product", "summary": "disagreement", "source_ids": [ids()["home"]]}
        ],
    }
    error = expect_rejection(payload)
    assert "at least two sources" in error.errors[0]


def test_every_problem_is_reported_at_once_for_the_retry() -> None:
    """The single retry gets the full list, not the first failure."""
    error = expect_rejection(
        {
            "claims": [
                claim_payload("src-ffffffffffff"),
                claim_payload(ids()["home"], excerpt="a quote the page never contained"),
            ],
            "unknowns": [],
            "conflicts": [],
        }
    )
    assert len(error.errors) == 2


# -- derived identifiers -----------------------------------------------------


def test_claim_ids_are_derived_and_deterministic() -> None:
    first = validate({"claims": [claim_payload(ids()["home"])], "unknowns": [], "conflicts": []})
    second = validate({"claims": [claim_payload(ids()["home"])], "unknowns": [], "conflicts": []})
    assert first.dossier.claims[0].claim_id == second.dossier.claims[0].claim_id
    assert first.dossier.claims[0].claim_id.startswith("ev-")


def test_a_model_supplied_claim_id_is_ignored() -> None:
    """Identity is earned by content, not asserted by the witness."""
    payload = claim_payload(ids()["home"])
    payload["claim_id"] = "ev-deadbeefdead"
    dossier = validate({"claims": [payload], "unknowns": [], "conflicts": []}).dossier
    assert dossier.claims[0].claim_id != "ev-deadbeefdead"


def test_different_claims_get_different_ids() -> None:
    payload = {
        "claims": [
            claim_payload(ids()["home"]),
            claim_payload(
                ids()["home"],
                excerpt="Plans start at 49 dollars per month",
                claim="The company publishes a starting price of 49 dollars per month.",
            ),
        ],
        "unknowns": [],
        "conflicts": [],
    }
    dossier = validate(payload).dossier
    assert len({claim.claim_id for claim in dossier.claims}) == 2


# -- normalisation -----------------------------------------------------------


def test_whitespace_normalisation_is_the_only_transformation() -> None:
    assert normalize_for_match("a\n\n  b\tc") == "a b c"
    # Non-breaking and narrow spaces are whitespace and collapse like any other.
    assert normalize_for_match("a b c") == "a b c"
    # Case and punctuation are preserved: an excerpt is a quotation.
    assert normalize_for_match("Don't Shout") == "Don't Shout"


def test_an_excerpt_matches_across_reflowed_whitespace() -> None:
    payload = claim_payload(
        ids()["home"], excerpt="reconciles   invoices\nfor plumbing contractors"
    )
    dossier = validate({"claims": [payload], "unknowns": [], "conflicts": []}).dossier
    assert dossier.claims[0].excerpts[0].excerpt == "reconciles invoices for plumbing contractors"


# -- coverage reporting ------------------------------------------------------


def test_coverage_and_confidence_inputs_are_counts_not_judgments() -> None:
    payload = {
        "claims": [claim_payload(ids()["home"])],
        "unknowns": [{"category": "team", "question": "Who are the founders?"}],
        "conflicts": [],
    }
    dossier = validate(payload).dossier

    assert dossier.source_coverage is not None
    assert dossier.source_coverage.sources_supplied == 2
    assert dossier.source_coverage.sources_cited == 1

    inputs = dossier.confidence_inputs
    assert inputs is not None
    assert inputs.claims_total == 1
    assert inputs.unknowns == 1
    assert inputs.claims_by_verification == {"company_claim": 1}
    # No score, band or recommendation is produced here.
    assert not hasattr(inputs, "score")


# -- typographic punctuation -------------------------------------------------
#
# Regression cover for the defect found by the first live run: a source that wrote
# "we’re" (U+2019) and a model that quoted "we're" (U+0027) were treated as different
# text, which rejected a fully supported nine-claim dossier over one character.

CURLY_SOURCE = (
    "Today, we’re launching Budibase Agents into Beta, empowering our users to build "
    "custom agents with their own models, APIs, and data. The team said “this is the "
    "biggest release yet” — a genuine milestone – and more is coming…"
)


def with_source(text: str) -> list[SuppliedSource]:
    reference = SourceReference.create("https://typo.example/", kind=SourceKind.COMPANY_PAGE)
    return [SuppliedSource(reference=reference, text=text, role="homepage")]


def accepts(source_text: str, excerpt: str) -> bool:
    """Whether an excerpt validates against a source consisting only of ``source_text``."""
    sources = with_source(source_text)
    payload = {
        "claims": [claim_payload(sources[0].source_id, excerpt=excerpt)],
        "unknowns": [],
        "conflicts": [],
    }
    try:
        validate(payload, sources=sources)
    except EvidenceValidationError:
        return False
    return True


@pytest.mark.parametrize(
    ("source_text", "excerpt", "why"),
    [
        ("The founder says we’re shipping today.", "we're shipping today", "curly -> straight"),
        ("The founder says we're shipping today.", "we’re shipping today", "straight -> curly"),
        ("They call it “agentic” software now.", '"agentic" software now', "curly doubles"),
        ('They call it "agentic" software now.', "“agentic” software now", "straight doubles"),
        ("A milestone – and a big one for us.", "milestone - and a big one", "en dash"),
        ("A milestone — and a big one for us.", "milestone - and a big one", "em dash"),
        ("and much more is coming… soon", "more is coming... soon", "ellipsis"),
        ("and much more is coming... soon", "more is coming… soon", "ellipsis reversed"),
        ("A ‘quoted’ phrase appears here.", "'quoted' phrase appears here", "single quotes"),
    ],
)
def test_typographic_variants_of_the_same_punctuation_match(
    source_text: str, excerpt: str, why: str
) -> None:
    assert accepts(source_text, excerpt), why


def test_the_exact_budibase_excerpt_now_validates() -> None:
    """The excerpt that failed twice on the live run, verbatim."""
    excerpt = (
        "we're launching Budibase Agents into Beta, empowering our users to build custom "
        "agents with their own models, APIs, and data"
    )
    assert accepts(CURLY_SOURCE, excerpt)


@pytest.mark.parametrize(
    ("excerpt", "why"),
    [
        ("we are launching Budibase Agents into Beta", "paraphrase: we're -> we are"),
        ("launching Budibase Agents into beta", "case change: Beta -> beta"),
        ("Budibase Agents launching into Beta", "word reordering"),
        ("launching Budibase Copilots into Beta", "substituted word"),
        ("empowering our customers to build custom agents", "substituted word mid-phrase"),
    ],
)
def test_punctuation_folding_does_not_admit_a_paraphrase(excerpt: str, why: str) -> None:
    """Folding rendering variants must not have loosened the quotation requirement."""
    assert not accepts(CURLY_SOURCE, excerpt), why


def test_only_the_seven_documented_characters_are_folded() -> None:
    """Other Unicode punctuation is left alone, so the fold stays a closed set."""
    # A guillemet is not in the map and must not be treated as a double quote.
    assert not accepts("They call it «agentic» software now.", '"agentic" software now')


def test_an_exact_ascii_excerpt_still_passes() -> None:
    assert accepts("Plans start at $49/mo, no setup fee.", "Plans start at $49/mo")


def test_normalisation_output_is_ascii_folded() -> None:
    assert normalize_for_match("we’re “here” — now…") == 'we\'re "here" - now...'


# -- closest-span diagnostic -------------------------------------------------


def test_a_mismatch_error_shows_the_closest_span_from_the_correct_source() -> None:
    sources = with_source("Acme Ops reconciles invoices for plumbing contractors nationwide.")
    payload = {
        "claims": [
            claim_payload(
                sources[0].source_id,
                excerpt="Acme Ops reconciles invoices for electricians nationwide",
            )
        ],
        "unknowns": [],
        "conflicts": [],
    }
    with pytest.raises(EvidenceValidationError) as caught:
        validate(payload, sources=sources)

    message = caught.value.errors[0]
    assert "The closest text in that source is:" in message
    assert "reconciles invoices for plumbing" in message


def test_the_diagnostic_span_is_bounded() -> None:
    sources = with_source(
        "Acme Ops reconciles invoices. " + "filler text about the product. " * 200
    )
    payload = {
        "claims": [
            claim_payload(sources[0].source_id, excerpt="Acme Ops reconciles nothing at all")
        ],
        "unknowns": [],
        "conflicts": [],
    }
    with pytest.raises(EvidenceValidationError) as caught:
        validate(payload, sources=sources)

    span = caught.value.errors[0].split('The closest text in that source is: "')[1].rstrip('"')
    assert len(span) <= 160


def test_the_diagnostic_cannot_quote_another_source_or_candidate() -> None:
    """The span comes only from the source the excerpt was attached to."""
    mine = SourceReference.create("https://mine.example/", kind=SourceKind.COMPANY_PAGE)
    other = SourceReference.create("https://other.example/", kind=SourceKind.COMPANY_PAGE)
    sources = [
        SuppliedSource(
            reference=mine, text="Our product reconciles invoices for plumbers.", role="homepage"
        ),
        SuppliedSource(
            reference=other,
            text="CONFIDENTIAL OTHER CANDIDATE TEXT that must never be quoted back.",
            role="homepage",
        ),
    ]
    payload = {
        "claims": [
            claim_payload(mine.source_id, excerpt="Our product reconciles ledgers for plumbers")
        ],
        "unknowns": [],
        "conflicts": [],
    }
    with pytest.raises(EvidenceValidationError) as caught:
        validate(payload, sources=sources)

    message = caught.value.errors[0]
    assert "CONFIDENTIAL OTHER CANDIDATE TEXT" not in message
    assert "Our product reconciles" in message


def test_a_wrong_source_attribution_still_names_the_right_source() -> None:
    """The pre-existing hint wins over the span when the text exists elsewhere."""
    mine = SourceReference.create("https://mine.example/", kind=SourceKind.COMPANY_PAGE)
    other = SourceReference.create("https://other.example/", kind=SourceKind.COMPANY_PAGE)
    sources = [
        SuppliedSource(
            reference=mine, text="Nothing relevant on this page at all.", role="homepage"
        ),
        SuppliedSource(
            reference=other, text="Plans start at $49 a month with no setup fee.", role="pricing"
        ),
    ]
    payload = {
        "claims": [claim_payload(mine.source_id, excerpt="Plans start at $49 a month")],
        "unknowns": [],
        "conflicts": [],
    }
    with pytest.raises(EvidenceValidationError) as caught:
        validate(payload, sources=sources)

    message = caught.value.errors[0]
    assert f"It does appear in {other.source_id}" in message
    assert "closest text" not in message


def test_a_wholly_unrelated_excerpt_keeps_the_generic_message() -> None:
    sources = with_source("Acme Ops reconciles invoices for plumbing contractors.")
    payload = {
        "claims": [
            claim_payload(sources[0].source_id, excerpt="Quarterly revenue reached forty million")
        ],
        "unknowns": [],
        "conflicts": [],
    }
    with pytest.raises(EvidenceValidationError) as caught:
        validate(payload, sources=sources)

    message = caught.value.errors[0]
    assert "Copy the excerpt verbatim from the supplied text." in message
    assert "closest text" not in message


def test_closest_span_is_deterministic() -> None:
    source = normalize_for_match("Acme Ops reconciles invoices for plumbing contractors.")
    excerpt = normalize_for_match("Acme Ops reconciles invoices for electricians")
    assert closest_span(excerpt, source) == closest_span(excerpt, source)


def test_closest_span_declines_a_short_or_unanchored_excerpt() -> None:
    source = normalize_for_match("Acme Ops reconciles invoices for plumbing contractors.")
    assert closest_span("tiny", source) is None
    assert closest_span(normalize_for_match("nothing here matches at all"), source) is None
