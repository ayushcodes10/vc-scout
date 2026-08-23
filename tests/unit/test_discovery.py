"""Discovery rules: URL acceptance, website canonicalisation, relevance and quality.

The relevance tests are the load-bearing ones. The first sourcing formula scored a story
by flat token overlap, which gave 12 of 15 shortlisted candidates the identical value
0.50 and let HN engagement decide the ranking outright. These pin the behaviour that
replaced it.
"""

from __future__ import annotations

import pytest

from vc_scout.discovery import (
    MIN_RELEVANCE_SCORE,
    RejectionReason,
    RelevanceClass,
    accept_product_url,
    canonical_website,
    classify_relevance,
    discovery_rank,
    engagement_score,
    parse_story_title,
    quality_score,
    recency_score,
)
from vc_scout.models.discovery import DISCOVERY_FORMULA_VERSION


def classify(title: str, one_liner: str | None = None, url: str = "https://neutral-co.example/"):
    return classify_relevance(title=title, one_liner=one_liner, url=url)


# -- URL acceptance ----------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://acmeops.example/",
        "https://www.ledgerly.example/pricing",
        "http://shiftpilot.example",
    ],
)
def test_product_urls_are_accepted(url: str) -> None:
    accepted, reason = accept_product_url(url)
    assert accepted
    assert reason is None


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("https://github.com/x/y", RejectionReason.BLOCKED_DOMAIN),
        ("https://someone.github.io/proj", RejectionReason.BLOCKED_DOMAIN),
        ("https://news.ycombinator.com/item?id=1", RejectionReason.BLOCKED_DOMAIN),
        ("https://techcrunch.com/story", RejectionReason.BLOCKED_DOMAIN),
        ("https://deepmind.google/discover/blog/agent", RejectionReason.INCUMBENT),
        ("https://openai.com/index/agents", RejectionReason.INCUMBENT),
        ("javascript:alert(1)", RejectionReason.UNSAFE_URL),
        ("/relative", RejectionReason.UNSAFE_URL),
        ("", RejectionReason.NO_URL),
    ],
)
def test_non_product_urls_are_rejected_with_a_reason(url: str, reason: str) -> None:
    accepted, actual = accept_product_url(url)
    assert not accepted
    assert actual == reason


# -- website canonicalisation ------------------------------------------------


@pytest.mark.parametrize(
    ("launch_url", "expected"),
    [
        ("https://acmeops.example/blog/launch-post", "https://acmeops.example/"),
        ("https://acmeops.example/research", "https://acmeops.example/"),
        ("https://app.teamout.example/ai", "https://app.teamout.example/"),
        ("https://acmeops.example/p?utm_source=hn&id=2", "https://acmeops.example/"),
    ],
)
def test_a_subpath_launch_url_is_reduced_to_its_origin(launch_url: str, expected: str) -> None:
    website, note = canonical_website(launch_url)
    assert website == expected
    assert note is not None and "preserved as a source" in note


@pytest.mark.parametrize(
    "launch_url", ["https://acmeops.example/", "https://acmeops.example", "http://acmeops.example/"]
)
def test_a_root_launch_url_is_left_alone(launch_url: str) -> None:
    website, note = canonical_website(launch_url)
    assert website.endswith("acmeops.example/")
    assert note is None


def test_canonicalisation_never_changes_the_host() -> None:
    """Dropping a path can never turn a link into a different company's website."""
    for url in ("https://sub.acmeops.example/blog/x", "https://acmeops.example/a/b/c"):
        website, _ = canonical_website(url)
        assert website.split("/")[2] == url.split("/")[2]


# -- relevance classification ------------------------------------------------


def test_ai_automation_plus_a_buyer_is_direct() -> None:
    relevance, score, _, matched = classify(
        "Show HN: Ledgerly - AI bookkeeping for small business owners"
    )
    assert relevance is RelevanceClass.DIRECT
    assert matched["ai"] and matched["buyer"]
    assert score > MIN_RELEVANCE_SCORE


def test_only_ai_agents_is_adjacent_not_direct() -> None:
    """The exact failure mode of the first formula: generic infrastructure scoring as
    though it were an on-topic workflow product."""
    relevance, score, _, matched = classify(
        "Show HN: AgentMesh - the fastest runtime for AI agents"
    )
    assert relevance is RelevanceClass.ADJACENT
    assert matched["ai"]
    assert matched["buyer"] == []
    assert matched["workflow"] == []
    assert score < classify("Show HN: AI agent for customer support")[1]


def test_ai_plus_customer_support_is_direct_without_saying_smb() -> None:
    relevance, _, _, matched = classify(
        "Launch HN: Helpdeskly - AI agent for customer support tickets"
    )
    assert relevance is RelevanceClass.DIRECT
    assert "customer support" in matched["workflow"] or "support" in matched["workflow"]
    assert "smb" not in matched["buyer"]


@pytest.mark.parametrize(
    "title",
    [
        "Show HN: Reconcile - automated accounting reconciliation",
        "Show HN: ShiftPilot - AI scheduling and booking for salons",
        "Show HN: an AI agent that handles invoicing",
        "Show HN: agentic payroll automation",
    ],
)
def test_ai_plus_a_named_workflow_is_direct(title: str) -> None:
    relevance, _, _, matched = classify(title)
    assert relevance is RelevanceClass.DIRECT
    assert matched["workflow"]


@pytest.mark.parametrize(
    "title",
    [
        "Show HN: PaperTrail - bookkeeping spreadsheets for small business",
        "Show HN: a scheduling app for restaurants",
        "Show HN: invoicing for contractors, built in Rust",
    ],
)
def test_a_business_tool_without_an_ai_signal_is_never_direct(title: str) -> None:
    """The buyer and workflow groups qualify an AI product; they do not substitute for one."""
    relevance, _, _, matched = classify(title)
    assert relevance is RelevanceClass.IRRELEVANT
    assert matched["ai"] == []


def test_a_story_with_no_signal_at_all_is_irrelevant() -> None:
    relevance, _, _, _ = classify("Show HN: a knitting pattern generator")
    assert relevance is RelevanceClass.IRRELEVANT


def test_direct_scores_above_adjacent_for_comparable_wording() -> None:
    generic = classify("Show HN: an AI agent platform")[1]
    workflow = classify("Show HN: an AI agent for invoicing")[1]
    buyer_and_workflow = classify("Show HN: an AI agent for invoicing for small business")[1]
    assert generic < workflow < buyer_and_workflow


def test_generic_wording_cannot_reach_the_score_of_a_qualified_product() -> None:
    """A candidate matching only "AI" and "agents" must not tie one that names a workflow."""
    stuffed = classify("Show HN: AI agent agents agentic automation copilot assistant")[1]
    qualified = classify("Show HN: AI agent for invoicing for small business")[1]
    assert stuffed < qualified


def test_relevance_reads_the_one_liner_and_url_path() -> None:
    from_path = classify("Show HN: Acme", url="https://acme.example/ai-agents-for-invoicing-smb")
    assert from_path[0] is RelevanceClass.DIRECT

    from_one_liner = classify("Show HN: Acme", one_liner="AI agent for restaurant scheduling")
    assert from_one_liner[0] is RelevanceClass.DIRECT


def test_a_top_level_domain_is_not_an_ai_signal() -> None:
    """A ".ai" TLD is a fact about domain fashion, not about the product."""
    relevance, _, _, matched = classify(
        "Show HN: a local model runner", url="https://lmstudio.ai/blog"
    )
    assert matched["ai"] == []
    assert relevance is RelevanceClass.IRRELEVANT


def test_matched_terms_are_reported_for_every_group() -> None:
    _, _, components, matched = classify(
        "Show HN: AI agent for invoicing for small business merchants"
    )
    assert set(matched) == {"ai", "buyer", "workflow"}
    assert set(components) == {"ai", "buyer", "workflow"}
    assert all(0.0 <= value <= 1.0 for value in components.values())


def test_classification_is_deterministic() -> None:
    title = "Show HN: Acme - AI agent for scheduling"
    assert classify(title) == classify(title)


# -- quality (tie-breaking only) ---------------------------------------------


def test_engagement_is_monotonic_and_bounded() -> None:
    assert engagement_score(0, 0) == 0.0
    assert engagement_score(10, 0) < engagement_score(100, 0)
    assert 0.0 <= engagement_score(100_000, 100_000) <= 1.0


def test_comments_count_more_than_points() -> None:
    assert engagement_score(0, 10) > engagement_score(10, 0)


@pytest.mark.parametrize(
    ("age_days", "expected"), [(0.0, 1.0), (30.0, 1.0), (720.0, 0.0), (5000.0, 0.0)]
)
def test_recency_bounds(age_days: float, expected: float) -> None:
    assert recency_score(age_days) == pytest.approx(expected)


def test_quality_is_a_documented_weighted_sum() -> None:
    score, parts = quality_score(points=212, num_comments=88, age_days=12.0, variant_weight=0.9)
    expected = 0.55 * parts["engagement"] + 0.25 * parts["recency"] + 0.20 * parts["variant"]
    assert score == pytest.approx(expected, abs=5e-4)


def test_quality_is_bounded() -> None:
    floor, _ = quality_score(points=0, num_comments=0, age_days=9999.0, variant_weight=0.0)
    ceiling, _ = quality_score(points=99999, num_comments=99999, age_days=0.0, variant_weight=1.0)
    assert floor == 0.0
    assert ceiling == pytest.approx(1.0)


# -- rank record -------------------------------------------------------------


def rank(title: str, *, url: str, points: int, comments: int, weight: float = 0.9):
    return discovery_rank(
        title=title,
        one_liner=None,
        url=url,
        points=points,
        num_comments=comments,
        age_days=20.0,
        variant_weight=weight,
    )


def test_class_outranks_quality_however_large_the_engagement_gap() -> None:
    """The regression this whole rework exists for."""
    quiet_direct = rank(
        "Show HN: an AI agent for invoicing for small business",
        url="https://quiet.example/",
        points=3,
        comments=1,
    )
    loud_adjacent = rank(
        "Show HN: the fastest runtime for AI agents",
        url="https://loud.example/",
        points=100_000,
        comments=50_000,
    )
    assert quiet_direct.class_rank > loud_adjacent.class_rank
    assert loud_adjacent.quality_score > quiet_direct.quality_score
    # Ordering is lexicographic, so the quality gap cannot cross the class boundary.
    assert _order(quiet_direct) < _order(loud_adjacent)


def _order(record) -> tuple[int, float, float]:
    return (-record.class_rank, -record.relevance_score, -record.quality_score)


def test_rank_carries_its_version_and_every_component() -> None:
    record = rank(
        "Show HN: AI agent for scheduling", url="https://a.example/", points=5, comments=2
    )
    assert record.formula_version == DISCOVERY_FORMULA_VERSION
    assert DISCOVERY_FORMULA_VERSION != "1.0.0"
    assert set(record.components) >= {
        "relevance_ai",
        "relevance_buyer",
        "relevance_workflow",
        "engagement",
        "recency",
        "variant",
    }


# -- title parsing -----------------------------------------------------------


@pytest.mark.parametrize(
    ("title", "name", "one_liner"),
    [
        (
            "Show HN: Acme Ops - an AI agent that reconciles invoices",
            "Acme Ops",
            "an AI agent that reconciles invoices",
        ),
        ("Show HN: Ledgerly (YC W25) - automated bookkeeping", "Ledgerly", "automated bookkeeping"),
        (
            "Launch HN: Cliniqa (YC S25) - AI front desk for dental practices",
            "Cliniqa",
            "AI front desk for dental practices",
        ),
        (
            "LM Studio Bionic: the AI agent for open models",
            "LM Studio Bionic",
            "the AI agent for open models",
        ),
        (
            "Show HN: Maritime, a platform for running AI agents",
            "Maritime",
            "a platform for running AI agents",
        ),
        ("Show HN: ShiftPilot", "ShiftPilot", None),
    ],
)
def test_parse_story_title(title: str, name: str, one_liner: str | None) -> None:
    assert parse_story_title(title) == (name, one_liner)


def test_a_sentence_is_not_treated_as_a_company_name() -> None:
    name, one_liner = parse_story_title(
        "Ops Copilot lets small builders automate quoting end to end"
    )
    assert name == ""
    assert one_liner is not None
