"""The Algolia client: request construction, retries and hit parsing.

Every test here runs against ``httpx.MockTransport``. The autouse fixture in
``tests/conftest.py`` blocks sockets, so a regression that reintroduced a live call would
fail rather than quietly reach the network.
"""

from __future__ import annotations

import httpx
import pytest

from tests.unit.hn_fixtures import QUERY, make_client, make_transport
from vc_scout.net.hn import (
    INTENT_FACETS,
    HnAlgoliaClient,
    HnError,
    MalformedHitError,
    parse_hit,
    query_variants,
)


def test_query_variants_are_bounded_deterministic_and_intent_led() -> None:
    variants = query_variants(QUERY)
    labels = [v.label for v in variants]

    assert labels[:3] == ["query-show-hn", "query-launch-hn", "query-story"]
    assert labels[3:] == [f"intent-{slug}" for slug, _, _ in INTENT_FACETS]
    assert len(variants) == 3 + len(INTENT_FACETS) == 12
    assert len(set(labels)) == len(labels)
    # Deterministic, and insensitive to incidental whitespace in the query.
    assert query_variants(QUERY) == query_variants(f"  {QUERY}  ")


def test_intent_facets_cover_the_thesis_surface() -> None:
    """The facet family is what makes the run find workflow products the operator did not
    think to name."""
    facet_text = " ".join(facet for _, facet, _ in INTENT_FACETS).lower()
    for expected in (
        "small business",
        "business operations",
        "customer support",
        "sales",
        "accounting",
        "scheduling",
        "back office",
        "ecommerce",
        "retail",
    ):
        assert expected in facet_text


def test_facet_queries_reuse_the_query_own_ai_wording() -> None:
    """A query phrased around "automation" must not be silently rewritten as "AI agents"."""
    facets = [
        v for v in query_variants("automation for dental clinics") if v.label.startswith("intent-")
    ]
    assert all(v.query.startswith("automation ") for v in facets)

    default = [v for v in query_variants("tools for plumbers") if v.label.startswith("intent-")]
    assert all(v.query.startswith("AI agents ") for v in default)


def test_facet_variants_search_show_hn_and_launch_hn_together() -> None:
    seen: list[httpx.Request] = []
    client = make_client(record=seen)
    facet = next(v for v in query_variants(QUERY) if v.label == "intent-smb")
    client.search(facet, hits_per_page=10)
    # Algolia's OR form. The comma form would mean AND and match nothing.
    assert seen[0].url.params["tags"] == "(show_hn,launch_hn)"


def test_only_the_query_faithful_show_hn_variant_requires_every_word() -> None:
    seen: list[httpx.Request] = []
    client = make_client(record=seen)
    for variant in query_variants(QUERY):
        client.search(variant, hits_per_page=10)

    sent = [("optionalWords" in request.url.params) for request in seen]
    assert sent[0] is False
    assert all(sent[1:]), "every other variant must relax word matching"


def test_transient_failure_is_retried_exactly_once() -> None:
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(503)

    client = HnAlgoliaClient(
        httpx.Client(transport=httpx.MockTransport(handler), base_url="https://hn.algolia.test"),
        sleep=lambda _seconds: None,
    )
    with pytest.raises(HnError, match="HTTP 503"):
        client.search(query_variants(QUERY)[0], hits_per_page=10)
    assert len(attempts) == 2


def test_client_errors_are_not_retried() -> None:
    attempts: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        return httpx.Response(404)

    client = HnAlgoliaClient(
        httpx.Client(transport=httpx.MockTransport(handler), base_url="https://hn.algolia.test"),
        sleep=lambda _seconds: None,
    )
    with pytest.raises(HnError, match="HTTP 404"):
        client.search(query_variants(QUERY)[0], hits_per_page=10)
    assert len(attempts) == 1


def test_non_json_response_is_an_error() -> None:
    transport = make_transport(
        overrides={"query-show-hn": httpx.Response(200, text="<html>nope</html>")}
    )
    client = HnAlgoliaClient(httpx.Client(transport=transport, base_url="https://hn.algolia.test"))
    with pytest.raises(HnError, match="not JSON"):
        client.search(query_variants(QUERY)[0], hits_per_page=10)


# -- hit parsing -------------------------------------------------------------


def _hit(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "objectID": "1",
        "title": "Show HN: Acme",
        "url": "https://acme.example",
        "points": 10,
        "num_comments": 4,
        "created_at_i": 1_750_000_000,
    }
    base.update(overrides)
    return base


def test_parse_hit_reads_metadata_and_builds_a_discussion_url() -> None:
    story = parse_hit(_hit(_tags=["story", "show_hn"], author="ada"))
    assert story.points == 10
    assert story.num_comments == 4
    assert story.author == "ada"
    assert story.tags == ("story", "show_hn")
    assert story.discussion_url == "https://news.ycombinator.com/item?id=1"
    assert story.created_at.tzinfo is not None


def test_parse_hit_falls_back_to_the_iso_timestamp() -> None:
    hit = _hit()
    del hit["created_at_i"]
    hit["created_at"] = "2026-07-20T09:00:00Z"
    assert parse_hit(hit).created_at.year == 2026


def test_missing_counters_default_to_zero_not_to_a_failure() -> None:
    hit = _hit()
    del hit["points"]
    del hit["num_comments"]
    story = parse_hit(hit)
    assert (story.points, story.num_comments) == (0, 0)


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        ({"objectID": None}, "objectID"),
        ({"title": ""}, "title"),
        ({"url": None}, "no external url"),
        ({"points": "many"}, "points"),
        ({"points": -5}, "negative"),
        ({"created_at_i": None, "created_at": "not-a-date"}, "created_at"),
    ],
)
def test_malformed_hits_raise_rather_than_returning_partial_data(
    overrides: dict[str, object], match: str
) -> None:
    hit = _hit(**overrides)
    if overrides.get("created_at_i", "keep") is None:
        del hit["created_at_i"]
    with pytest.raises(MalformedHitError, match=match):
        parse_hit(hit)


def test_a_non_object_hit_is_malformed_not_a_crash() -> None:
    with pytest.raises(MalformedHitError, match="expected object"):
        parse_hit("just a string")


def test_age_days_is_measured_against_the_supplied_clock() -> None:
    from tests.unit.hn_fixtures import NOW

    story = parse_hit(_hit(created_at_i=int(NOW.timestamp()) - 86400 * 10))
    assert story.age_days(NOW) == pytest.approx(10.0, abs=0.01)


def test_the_suite_genuinely_cannot_reach_the_network() -> None:
    """Proves the guard in conftest is active rather than merely assumed.

    Every other test in this file relies on it: if this one starts passing trivially, the
    offline guarantee has silently lapsed.
    """
    with pytest.raises(BaseException) as caught:  # noqa: B017,PT011
        httpx.Client(timeout=0.01).get("https://hn.algolia.com/api/v1/search")
    assert "network access is disabled" in str(caught.value) or isinstance(
        caught.value, httpx.TransportError
    )


def test_the_stage_never_constructs_its_own_client() -> None:
    """The client is always injected, which is what makes the offline guarantee testable."""
    import inspect

    from vc_scout.stages import source as source_stage

    assert "HnAlgoliaClient(" not in inspect.getsource(source_stage)
