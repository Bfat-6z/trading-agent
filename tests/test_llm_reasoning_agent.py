import json
from pathlib import Path

import llm_reasoning_agent as lra

def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

def test_sanitize_reasoning_forces_read_only_contract():
    payload = {
        "summary": "ok",
        "risk_proposal": {"can_place_live_orders": True, "can_loosen_risk": True, "mode": "risk_on"},
    }

    result = lra.sanitize_reasoning(payload, {"provider": "9router", "deep_model": "gpt-5.5"}, "raw")

    assert result["risk_proposal"]["mode"] == "tighten_only"
    assert result["risk_proposal"]["can_place_live_orders"] is False
    assert result["risk_proposal"]["can_loosen_risk"] is False
    assert result["contract"]["can_place_live_orders"] is False
    assert "model_attempted_live_order_permission" in result["safety_violations_corrected"]

def test_run_once_calls_large_model_and_writes_outputs(tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    memory = state / "agent_memory"
    now = lra.utc_now()
    write_json(memory / "execution_bias.json", {"min_signal_score": 8})
    write_json(state / "market_updates_latest.json", {"ts": now, "hot": [{"symbol": "BTCUSDT"}]})
    write_json(memory / "news_latest.json", {"ts": now})
    write_json(memory / "shadow_performance_latest.json", {"overall": {"closed": 10, "expectancy": -0.01}})
    write_json(memory / "daily_exam_latest.json", {"quality_score": 50})
    write_json(memory / "self_improvement_latest.json", {"overall_learning_score": 0.4})
    write_json(memory / "cognitive_state_latest.json", {"reasoning_trace": {"thought_quality_score": 0.5}})
    write_json(memory / "reasoning_trace_latest.json", {"decision": {"mode": "sleep_observe_and_shadow"}})
    write_json(memory / "setup_skills.json", {"skills": {"exhaustion_fade": {"enabled": True, "stats": {"trades": 3, "expectancy": -0.01}}}})
    (state / "scalp_autotrader.jsonl").write_text('{"event":"paper_close","net":"-0.1"}\n', encoding="utf-8")

    monkeypatch.setattr(lra, "STATE_DIR", state)
    monkeypatch.setattr(lra, "MEMORY_DIR", memory)
    monkeypatch.setattr(lra, "MARKET_LATEST", state / "market_updates_latest.json")
    monkeypatch.setattr(lra, "SCALP_LOG", state / "scalp_autotrader.jsonl")
    monkeypatch.setattr(lra, "BIAS_PATH", memory / "execution_bias.json")
    monkeypatch.setattr(lra, "NEWS_LATEST", memory / "news_latest.json")
    monkeypatch.setattr(lra, "SHADOW_PERFORMANCE", memory / "shadow_performance_latest.json")
    monkeypatch.setattr(lra, "SELF_IMPROVEMENT", memory / "self_improvement_latest.json")
    monkeypatch.setattr(lra, "DAILY_EXAM", memory / "daily_exam_latest.json")
    monkeypatch.setattr(lra, "COGNITIVE_LATEST", memory / "cognitive_state_latest.json")
    monkeypatch.setattr(lra, "REASONING_TRACE", memory / "reasoning_trace_latest.json")
    monkeypatch.setattr(lra, "SETUP_SKILLS", memory / "setup_skills.json")
    monkeypatch.setattr(lra, "BELIEF_LEDGER", memory / "belief_ledger.json")
    monkeypatch.setattr(lra, "SEMANTIC_MEMORY", memory / "semantic_memory.json")
    monkeypatch.setattr(lra, "LATEST_JSON", memory / "llm_reasoning_latest.json")
    monkeypatch.setattr(lra, "HISTORY_JSONL", memory / "llm_reasoning_history.jsonl")
    monkeypatch.setattr(lra, "REPORT_MD", memory / "llm_reasoning_latest.md")
    monkeypatch.setattr(lra, "HEARTBEAT_PATH", state / "llm_reasoning_agent_heartbeat.json")
    monkeypatch.setattr(lra, "provider_snapshot", lambda: {"provider": "9router", "deep_model": "gpt-5.5", "quick_model": "gpt-5.5", "judge_model": "gpt-5.5"})
    monkeypatch.setattr(lra, "safe_append_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(lra, "safe_append_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(lra, "safe_upsert_heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        lra,
        "call_large_model",
        lambda *args, **kwargs: json.dumps({
            "summary": "Cần tiếp tục gom shadow data.",
            "market_read": "Market đủ mới nhưng edge yếu.",
            "critical_blindspots": ["negative_shadow_edge"],
            "hypotheses": [{"id": "h1", "setup_id": "exhaustion_fade", "statement": "Fade đang yếu", "test": "shadow only", "success_metric": "expectancy > 0"}],
            "paper_shadow_experiments": [],
            "risk_proposal": {"mode": "tighten_only", "can_place_live_orders": True, "can_loosen_risk": True, "min_signal_score": 8, "reason": "test"},
            "curriculum": [{"priority": 1, "task": "Backfill shadow", "acceptance_test": "closed >= 500"}],
            "confidence": 0.7,
        }),
    )

    result = lra.run_once(max_log_lines=20)

    assert result["status"] == "ok"
    assert result["provider"]["provider"] == "9router"
    assert result["reasoning"]["risk_proposal"]["can_place_live_orders"] is False
    assert result["reasoning"]["risk_proposal"]["can_loosen_risk"] is False
    assert lra.LATEST_JSON.exists()
    assert lra.HISTORY_JSONL.exists()
    assert lra.REPORT_MD.exists()
    assert lra.HEARTBEAT_PATH.exists()

def test_run_once_degrades_when_model_call_fails(tmp_path: Path, monkeypatch):
    memory = tmp_path / "memory"
    monkeypatch.setattr(lra, "LATEST_JSON", memory / "latest.json")
    monkeypatch.setattr(lra, "HISTORY_JSONL", memory / "history.jsonl")
    monkeypatch.setattr(lra, "REPORT_MD", memory / "latest.md")
    monkeypatch.setattr(lra, "HEARTBEAT_PATH", tmp_path / "hb.json")
    monkeypatch.setattr(lra, "provider_snapshot", lambda: {"provider": "9router", "deep_model": "gpt-5.5", "quick_model": "gpt-5.5"})
    monkeypatch.setattr(lra, "collect_context", lambda max_log_lines=80: {})
    monkeypatch.setattr(lra, "call_large_model", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("timeout")))
    monkeypatch.setattr(lra, "safe_append_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(lra, "safe_append_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(lra, "safe_upsert_heartbeat", lambda *args, **kwargs: None)

    result = lra.run_once()

    assert result["status"] == "degraded"
    assert result["reasoning"]["risk_proposal"]["can_place_live_orders"] is False
    assert "timeout" in result["error"]
