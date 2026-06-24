from pathlib import Path
import json

import self_improvement_agent as sia
import setup_skill_library as ssl


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_score_edge_quality_penalizes_negative_shadow_edge():
    score = sia.score_edge_quality(
        {"closes": 0, "win_rate": 0.0},
        {"overall": {"win_rate": 0.2, "expectancy": -0.01, "profit_factor": 0.4}},
        [],
    )

    assert score["score"] < 0.4
    assert score["shadow_expectancy"] < 0


def test_guardrail_proposal_never_allows_live_or_loosen():
    proposal = sia.build_guardrail_proposal(
        {"bias": {"min_signal_score": 7}},
        {"edge_quality": {"score": 0.2}},
        [{"severity": "critical", "type": "negative_shadow_edge"}],
    )

    assert proposal["can_loosen"] is False
    assert proposal["can_trade_live"] is False
    assert proposal["recommended_min_signal_score"] == 8
    assert proposal["requires_human_review"] is True


def test_run_once_writes_self_improvement_outputs(tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    memory = state / "agent_memory"
    scalp_log = state / "scalp_autotrader.jsonl"
    scalp_log.parent.mkdir(parents=True, exist_ok=True)
    scalp_log.write_text(
        '\n'.join([
            '{"event":"paper_close","net":"-0.1","symbol":"BTCUSDT"}',
            '{"event":"risk_block","reason":"memory_sleep"}',
        ])
        + "\n",
        encoding="utf-8",
    )
    write_json(memory / "execution_bias.json", {"min_signal_score": 7})
    write_json(memory / "market_model.json", {"last_market_state": {"primary_regime": "risk_on"}, "last_rules": {"min_signal_score": 8}})
    write_json(memory / "cognitive_state_latest.json", {"ts": sia.utc_now(), "reasoning_trace": {"thought_quality_score": 0.4, "missing_evidence": ["sample"], "contradictions": []}})
    write_json(memory / "shadow_performance_latest.json", {"overall": {"closed": 50, "win_rate": 0.2, "expectancy": -0.01, "profit_factor": 0.4}, "data_quality": {"confidence": "medium", "selected_rows": 60, "api_error_count": 3, "unresolved_count": 2}, "kill_candidates": [{"group": "by_symbol", "key": "BADUSDT", "closed": 30, "win_rate": 0.2, "expectancy": -0.01}]})
    write_json(memory / "news_latest.json", {"ts": sia.utc_now()})

    library = ssl.default_library()
    ssl.record_setup_outcome(library, "momentum_continuation", -0.1, "risk_on", "BTCUSDT", "LONG")
    monkeypatch.setattr(sia, "BIAS_PATH", memory / "execution_bias.json")
    monkeypatch.setattr(sia, "MARKET_MODEL_PATH", memory / "market_model.json")
    monkeypatch.setattr(sia, "COGNITIVE_LATEST", memory / "cognitive_state_latest.json")
    monkeypatch.setattr(sia, "REFLECTION_PROFILE", memory / "profile.json")
    monkeypatch.setattr(sia, "SHADOW_PERFORMANCE", memory / "shadow_performance_latest.json")
    monkeypatch.setattr(sia, "NEWS_LATEST", memory / "news_latest.json")
    monkeypatch.setattr(sia, "SCALP_LOG", scalp_log)
    monkeypatch.setattr(sia, "LATEST_JSON", memory / "self_improvement_latest.json")
    monkeypatch.setattr(sia, "HISTORY_JSONL", memory / "self_improvement_history.jsonl")
    monkeypatch.setattr(sia, "REPORT_MD", memory / "self_improvement_latest.md")
    monkeypatch.setattr(sia, "HEARTBEAT_PATH", state / "self_improvement_agent_heartbeat.json")
    monkeypatch.setattr(sia, "load_ledger", lambda: {"beliefs": {}, "history": []})
    monkeypatch.setattr(sia, "load_library", lambda: library)
    monkeypatch.setattr(sia, "safe_append_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(sia, "safe_append_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(sia, "safe_upsert_heartbeat", lambda *args, **kwargs: None)

    result = sia.run_once(max_log_lines=100)

    blindspot_types = {item["type"] for item in result["blindspots"]}
    assert "negative_shadow_edge" in blindspot_types
    assert "market_data_gap" in blindspot_types
    assert result["guardrail_proposal"]["can_loosen"] is False
    assert result["guardrail_proposal"]["can_trade_live"] is False
    assert any(task["task"] == "Freeze promotion" for task in result["learning_curriculum"])
    assert sia.LATEST_JSON.exists()
    assert sia.HISTORY_JSONL.exists()
    assert sia.REPORT_MD.exists()
    assert sia.HEARTBEAT_PATH.exists()
