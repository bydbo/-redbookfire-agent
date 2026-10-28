"""E9 开发集网格选参：误推荐门槛、质量次序与基线冠军。"""

from __future__ import annotations

from xhs_agent.evaluation.sweep import (
    FALSE_RECOMMENDATION_MAX,
    GridPoint,
    choose_baseline,
    choose_point,
    grid_points,
)


def test_grid_has_three_plus_three_plus_nine_points() -> None:
    points = grid_points()
    assert len(points) == 15
    assert len({point.label for point in points}) == 15
    assert sum(1 for point in points if point.strategy == "hybrid") == 9


def test_false_recommendation_gate_prefers_eligible_point() -> None:
    points = [
        GridPoint("hybrid", 0.2, 0.45, "high-quality-but-risky"),
        GridPoint("hybrid", 0.3, 0.6, "eligible"),
    ]
    metrics = {
        "high-quality-but-risky": {"ndcg": 0.9, "hit5": 0.9, "top1": 0.7,
                                   "false_recommendation_rate": 0.3},
        "eligible": {"ndcg": 0.6, "hit5": 0.8, "top1": 0.5,
                     "false_recommendation_rate": 0.2},
    }
    decision = choose_point(points, metrics)
    assert decision["point"].label == "eligible"
    assert decision["meets_false_recommendation"] is True


def test_no_eligible_point_is_marked_not_meeting() -> None:
    points = [GridPoint("hybrid", 0.5, 0.75, "bad")]
    decision = choose_point(points, {"bad": {
        "ndcg": 0.1, "hit5": 0.2, "top1": 0.1,
        "false_recommendation_rate": FALSE_RECOMMENDATION_MAX + 0.1}})
    assert decision["status"] == "not_meeting_false_recommendation"


def test_tie_breaks_by_hit5_then_top1_then_false_rate() -> None:
    points = [
        GridPoint("hybrid", 0.2, 0.45, "a"),
        GridPoint("hybrid", 0.3, 0.6, "b"),
        GridPoint("hybrid", 0.5, 0.75, "c"),
    ]
    metrics = {
        "a": {"ndcg": 0.6, "hit5": 0.9, "top1": 0.5, "false_recommendation_rate": 0.1},
        "b": {"ndcg": 0.6, "hit5": 0.8, "top1": 0.7, "false_recommendation_rate": 0.1},
        "c": {"ndcg": 0.6, "hit5": 0.9, "top1": 0.5, "false_recommendation_rate": 0.05},
    }
    assert choose_point(points, metrics)["point"].label == "c"


def test_baseline_champion_includes_keyword_and_selected_arms() -> None:
    selected = {
        "trgm_only": GridPoint("trgm_only", 0.3, 0.6, "trgm"),
        "vector_only": GridPoint("vector_only", 0.3, 0.6, "vector"),
    }
    metrics = {
        "keyword": {"ndcg": 0.4, "hit5": 0.7, "top1": 0.4,
                    "false_recommendation_rate": 0.1},
        "trgm": {"ndcg": 0.5, "hit5": 0.8, "top1": 0.5,
                 "false_recommendation_rate": 0.1},
        "vector": {"ndcg": 0.6, "hit5": 0.8, "top1": 0.5,
                   "false_recommendation_rate": 0.1},
    }
    decision = choose_baseline(selected, metrics)
    assert decision["strategy"] == "vector_only"
