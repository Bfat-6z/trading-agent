from pathlib import Path

import atomic_state
import test_result_memory_agent as trm

def test_test_result_memory_rules_create_curriculum_from_runtime_failures():
    sources = {
        "daily_exam": {"quality_score": 80},
        "counterfactual": {"coverage_pct": 0.12},
        "shadow": {"fresh_window": {"overall": {"expectancy": -0.01, "profit_factor": 0.4}}},
        "walk_forward": {"by_status": {"running": 1}},
        "promotion": {"state": "paper_learning", "passed": False},
        "learning_benchmark": {"score": 0.0, "lessons": [{"scenario_id": "s1", "name": "scenario", "expected_action": "skip", "actual_action": "paper_long", "lesson": "skip bad setup", "next_action": "tighten gate"}]},
    }

    lessons = trm.build_test_memory_lessons(sources)
    gaps = {row["gap"] for row in lessons}

    assert "counterfactual_coverage_low" in gaps
    assert "shadow_edge_weak" in gaps
    assert "walk_forward_not_done" in gaps
    assert "promotion_blocked" in gaps
    assert "scenario_mismatch" in gaps

def test_test_result_memory_run_once_writes_latest_and_episodes(monkeypatch, tmp_path: Path):
    memory = tmp_path / "agent_memory"
    state = tmp_path
    memory.mkdir()
    monkeypatch.setattr(trm, "MEMORY_DIR", memory)
    monkeypatch.setattr(trm, "STATE_DIR", state)
    monkeypatch.setattr(
        trm,
        "SOURCE_FILES",
        {
            "daily_exam": memory / "daily_exam_latest.json",
            "counterfactual": memory / "counterfactual_latest.json",
            "shadow": memory / "shadow_performance_latest.json",
            "walk_forward": memory / "walk_forward_latest.json",
            "promotion": memory / "promotion_board_latest.json",
            "learning_benchmark": memory / "learning_exam_benchmark_latest.json",
        },
    )
    monkeypatch.setattr(trm, "HEARTBEAT_PATH", state / "test_result_memory_agent_heartbeat.json")
    monkeypatch.setattr(trm, "record_episode", lambda **kwargs: {"last_episode": kwargs, "last_inserted": True})
    trm.write_json_atomic(trm.SOURCE_FILES["daily_exam"], {"quality_score": 50})
    trm.write_json_atomic(trm.SOURCE_FILES["counterfactual"], {"coverage_pct": 0.1})
    trm.write_json_atomic(trm.SOURCE_FILES["shadow"], {"overall": {"expectancy": -0.01, "profit_factor": 0.5}})
    trm.write_json_atomic(trm.SOURCE_FILES["walk_forward"], {"status": "running"})
    trm.write_json_atomic(trm.SOURCE_FILES["promotion"], {"state": "paper_learning", "passed": False})
    trm.write_json_atomic(trm.SOURCE_FILES["learning_benchmark"], {"score": 1.0})

    result = trm.run_once(output_path=memory / "test_result_memory_latest.json", history_path=memory / "test_result_memory_history.jsonl")

    assert result["lesson_count"] >= 5
    assert result["high_severity_count"] >= 3
    assert result["can_place_live_orders"] is False
    assert (memory / "test_result_memory_latest.json").exists()
    assert trm.HEARTBEAT_PATH.exists()

def test_test_result_memory_prioritizes_repeated_failures_from_history(monkeypatch, tmp_path: Path):
    memory = tmp_path / "agent_memory"
    state = tmp_path
    memory.mkdir()
    monkeypatch.setattr(trm, "MEMORY_DIR", memory)
    monkeypatch.setattr(trm, "STATE_DIR", state)
    monkeypatch.setattr(
        trm,
        "SOURCE_FILES",
        {
            "daily_exam": memory / "daily_exam_latest.json",
            "counterfactual": memory / "counterfactual_latest.json",
            "shadow": memory / "shadow_performance_latest.json",
            "walk_forward": memory / "walk_forward_latest.json",
            "promotion": memory / "promotion_board_latest.json",
            "learning_benchmark": memory / "learning_exam_benchmark_latest.json",
        },
    )
    monkeypatch.setattr(trm, "HEARTBEAT_PATH", state / "test_result_memory_agent_heartbeat.json")
    monkeypatch.setattr(trm, "record_episode", lambda **kwargs: {"last_episode": kwargs, "last_inserted": True})
    atomic_state.write_json_atomic(memory / "daily_exam_latest.json", {"quality_score": 40})
    atomic_state.write_json_atomic(memory / "counterfactual_latest.json", {"coverage_pct": 0.15})
    atomic_state.write_json_atomic(memory / "shadow_performance_latest.json", {"overall": {"expectancy": -0.02, "profit_factor": 0.6}})
    atomic_state.write_json_atomic(memory / "walk_forward_latest.json", {"status": "running"})
    atomic_state.write_json_atomic(memory / "promotion_board_latest.json", {"state": "paper_learning", "passed": False})
    atomic_state.write_json_atomic(memory / "learning_exam_benchmark_latest.json", {"score": 1.0})
    atomic_state.append_jsonl(
        memory / "test_result_memory_history.jsonl",
        {
            "lesson_count": 2,
            "lessons": [
                {"gap": "counterfactual_coverage_low", "severity": "high", "source": "daily_exam", "lesson": "raise replay coverage", "next_action": "run replay"},
                {"gap": "promotion_blocked", "severity": "medium", "source": "promotion", "lesson": "keep paper only", "next_action": "stay blocked"},
            ],
        },
    )

    result = trm.run_once(output_path=memory / "test_result_memory_latest.json", history_path=memory / "test_result_memory_history.jsonl")

    assert result["history_count"] == 1
    assert result["priority_curriculum"][0]["gap"] == "counterfactual_coverage_low"
    assert result["priority_curriculum"][0]["occurrences"] >= 2
    assert result["gap_stats"][0]["gap"] == "counterfactual_coverage_low"
