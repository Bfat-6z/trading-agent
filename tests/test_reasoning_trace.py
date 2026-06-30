from pathlib import Path

from reasoning_trace import build_reasoning_trace, save_trace


def _base_state():
    return {
        "focus": {"focus_type": "under_sampled_setup", "setup_id": "exhaustion_fade"},
        "experiment_plan": {"mode": "sample_collection"},
        "paper": {"closed_window": 0, "wins": 0, "losses": 0, "net": 0.0},
        "belief_summary": {
            "top_beliefs": [
                {"belief_id": "b1", "statement": "Wait for evidence.", "confidence": 0.8, "status": "active"}
            ]
        },
    }


def test_sleep_mode_blocks_paper_and_preserves_next_actions():
    trace = build_reasoning_trace(
        _base_state(),
        {"ts": "2026-06-20T00:00:00+00:00", "hot": [{"symbol": "BTCUSDT"}]},
        {"sleep_until": "2099-01-01T00:00:00+00:00", "min_signal_score": 8, "blocked_sides": []},
        {"bias_patch": {"paper_candidates": [{"symbol": "BTCUSDT"}], "high_risk_count": 3}},
        {"hypotheses": []},
        {"latest": {"event_count": 10, "risk_blocks": {"memory_sleep": 2}}},
        ts="2026-06-20T00:00:00+00:00",
    )

    assert trace["decision"]["mode"] == "sleep_observe_and_shadow"
    assert trace["schema_version"] == 1
    assert trace["trace_schema_version"] == "reasoning_trace.v2"
    assert trace["trace_id"].startswith("reasoning_trace_")
    assert trace["input_hashes"]["market_snapshot"].startswith("sha256:")
    assert "2026-06-20T00:00:00+00:00" in trace["evidence_ids"]
    assert trace["decision"]["allow_paper_entry"] is False
    assert "do_not_open_paper_until_sleep_expires" in trace["next_actions"]
    assert "dream_has_paper_candidates_while_executor_is_asleep" in trace["contradictions"]


def test_blocked_side_hypothesis_creates_contradiction():
    trace = build_reasoning_trace(
        _base_state(),
        {"ts": "2026-06-20T00:00:00+00:00"},
        {"min_signal_score": 8, "blocked_sides": ["LONG"]},
        {"bias_patch": {"paper_candidates": [], "high_risk_count": 0}},
        {
            "hypotheses": [
                {
                    "hypothesis_id": "h1",
                    "setup_id": "momentum_continuation",
                    "symbols": ["BTCUSDT"],
                    "prediction": {"side": "LONG"},
                    "metrics": ["tp_before_sl"],
                    "invalidation": ["lost_level"],
                }
            ]
        },
        {"latest": {"event_count": 12}},
        ts="2026-06-20T00:00:00+00:00",
    )

    assert trace["decision"]["mode"] == "resolve_contradictions_first"
    assert "hypothesis_side_LONG_is_currently_blocked" in trace["contradictions"]

def test_replay_ts_controls_sleep_state_and_trace_id_stability():
    before = build_reasoning_trace(
        _base_state(),
        {"ts": "2026-06-20T00:00:00+00:00"},
        {"sleep_until": "2026-06-21T00:00:00+00:00", "min_signal_score": 8, "blocked_sides": []},
        {"bias_patch": {}},
        {"hypotheses": []},
        {"latest": {"event_count": 1}},
        ts="2026-06-20T12:00:00+00:00",
    )
    replay = build_reasoning_trace(
        _base_state(),
        {"ts": "2026-06-20T00:00:00+00:00"},
        {"sleep_until": "2026-06-21T00:00:00+00:00", "min_signal_score": 8, "blocked_sides": []},
        {"bias_patch": {}},
        {"hypotheses": []},
        {"latest": {"event_count": 1}},
        ts="2026-06-20T12:00:00+00:00",
    )
    after = build_reasoning_trace(
        _base_state(),
        {"ts": "2026-06-20T00:00:00+00:00"},
        {"sleep_until": "2026-06-21T00:00:00+00:00", "min_signal_score": 8, "blocked_sides": []},
        {"bias_patch": {}},
        {"hypotheses": []},
        {"latest": {"event_count": 1}},
        ts="2026-06-22T00:00:00+00:00",
    )

    assert before["decision"]["mode"] == "sleep_observe_and_shadow"
    assert before["trace_id"] == replay["trace_id"]
    assert after["decision"]["mode"] != "sleep_observe_and_shadow"


def test_save_trace_writes_json_markdown_and_history(tmp_path: Path, monkeypatch):
    monkeypatch.setattr("reasoning_trace.safe_append_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr("reasoning_trace.safe_append_event", lambda *args, **kwargs: None)
    trace = build_reasoning_trace(
        _base_state(),
        {"ts": "2026-06-20T00:00:00+00:00"},
        {"min_signal_score": 8, "blocked_sides": []},
        {"bias_patch": {}},
        {"hypotheses": []},
        {"latest": {"event_count": 1}},
        ts="2026-06-20T00:00:00+00:00",
    )
    latest = tmp_path / "reasoning_trace_latest.json"
    report = tmp_path / "reasoning_trace_latest.md"
    history = tmp_path / "reasoning_trace_history.jsonl"

    save_trace(trace, latest, report, history)

    assert latest.exists()
    assert report.exists()
    assert history.exists()
    assert "# Reasoning Trace" in report.read_text(encoding="utf-8")
