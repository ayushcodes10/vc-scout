"""The rubric is configuration, and configuration drift silently rescales every memo."""

from __future__ import annotations

from vc_scout.models.enums import RubricDimension
from vc_scout.rubric import MAX_TOTAL_SCORE, RUBRIC, RUBRIC_BY_KEY, max_points_for


def test_rubric_totals_one_hundred() -> None:
    assert MAX_TOTAL_SCORE == 100


def test_rubric_covers_every_dimension_exactly_once() -> None:
    keys = [spec.key for spec in RUBRIC]
    assert len(keys) == len(set(keys)) == len(RubricDimension)


def test_configured_weights_match_the_assignment() -> None:
    assert {spec.key.value: spec.max_points for spec in RUBRIC} == {
        "pain_roi": 20,
        "wedge": 15,
        "distribution": 15,
        "defensibility": 15,
        "team": 15,
        "traction": 10,
        "market_timing": 10,
    }


def test_max_points_for_reads_configuration() -> None:
    for spec in RUBRIC:
        assert max_points_for(spec.key) == RUBRIC_BY_KEY[spec.key].max_points
