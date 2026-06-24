import learning_exam_benchmark as bench

def test_learning_exam_benchmark_scores_default_scenarios_without_live_permission():
    result = bench.run_benchmark()

    assert result["scenario_count"] >= 5
    assert result["score"] == 1.0
    assert result["failed_count"] == 0
    assert result["can_place_live_orders"] is False
    assert result["can_loosen_risk"] is False

def test_learning_exam_benchmark_records_lessons_on_scenario_mismatch():
    scenarios = [
        {
            "scenario_id": "s1",
            "name": "bad_expectation",
            "setup_id": "funding_squeeze",
            "features": {"funding_pct": -0.3, "range_pos": 0.05, "quote_volume": 100_000_000, "btc_regime": "neutral"},
            "expected_action": "skip",
            "lesson_on_fail": "expected lesson",
            "next_action_on_fail": "expected action",
        }
    ]

    result = bench.run_benchmark(scenarios)

    assert result["score"] == 0.0
    assert result["failed_count"] == 1
    assert result["lessons"][0]["lesson"] == "expected lesson"
    assert result["lessons"][0]["actual_action"] == "paper_long"
