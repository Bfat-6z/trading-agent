import json
from pathlib import Path

import daily_exam_agent as dea
import setup_skill_library as ssl

def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")

def test_quality_rubric_rewards_risk_discipline_and_fresh_data():
    now = dea.utc_now()
    inputs = {
        "bias": {"min_signal_score": 8, "risk_posture": "defensive"},
        "market": {"ts": now},
        "news": {"ts": now},
        "cognitive": {"ts": now, "reasoning_trace": {"thought_quality_score": 0.8}},
        "self_improvement": {"ts": now, "overall_learning_score": 0.7, "guardrail_proposal": {"can_trade_live": False, "can_loosen": False}},
        "live_readiness": {"mode": "paper"},
        "shadow": {"overall": {"closed": 500, "win_rate": 0.55, "expectancy": 0.01, "profit_factor": 1.3}},
        "paper": {"closes": 50, "win_rate": 0.5},
        "setups": [{"trades": 25, "expectancy": 0.01, "win_rate": 0.55}],
        "previous_exam": {},
    }

    rubric = dea.quality_rubric(inputs)

    assert rubric["quality_score"] > 70
    assert rubric["scores"]["risk_discipline"]["score"] >= 0.9

def test_run_once_writes_paper_only_exam_outputs(tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    memory = state / "agent_memory"
    scalp_log = state / "scalp_autotrader.jsonl"
    scalp_log.parent.mkdir(parents=True, exist_ok=True)
    scalp_log.write_text('{"event":"paper_close","net":"0.1"}\n', encoding="utf-8")
    now = dea.utc_now()
    write_json(state / "market_updates_latest.json", {"ts": now, "hot": [{"symbol": "BTCUSDT", "change_pct": 22, "quote_volume": 100000000}]})
    write_json(memory / "execution_bias.json", {"min_signal_score": 8, "risk_posture": "defensive"})
    write_json(memory / "news_latest.json", {"ts": now, "macro_risk_score": 0.2, "headline_chaos": 0.1})
    write_json(memory / "shadow_performance_latest.json", {"overall": {"closed": 80, "win_rate": 0.4, "expectancy": -0.01, "profit_factor": 0.7}, "data_quality": {"confidence": "medium"}})
    write_json(memory / "self_improvement_latest.json", {"ts": now, "overall_learning_score": 0.5, "guardrail_proposal": {"can_trade_live": False, "can_loosen": False}})
    write_json(memory / "cognitive_state_latest.json", {"ts": now, "reasoning_trace": {"thought_quality_score": 0.6}})
    write_json(memory / "live_readiness_latest.json", {"mode": "paper"})
    library = ssl.default_library()

    monkeypatch.setattr(dea, "STATE_DIR", state)
    monkeypatch.setattr(dea, "MEMORY_DIR", memory)
    monkeypatch.setattr(dea, "MARKET_LATEST", state / "market_updates_latest.json")
    monkeypatch.setattr(dea, "SCALP_LOG", scalp_log)
    monkeypatch.setattr(dea, "BIAS_PATH", memory / "execution_bias.json")
    monkeypatch.setattr(dea, "NEWS_LATEST", memory / "news_latest.json")
    monkeypatch.setattr(dea, "SHADOW_PERFORMANCE", memory / "shadow_performance_latest.json")
    monkeypatch.setattr(dea, "SELF_IMPROVEMENT", memory / "self_improvement_latest.json")
    monkeypatch.setattr(dea, "COGNITIVE_LATEST", memory / "cognitive_state_latest.json")
    monkeypatch.setattr(dea, "LIVE_READINESS", memory / "live_readiness_latest.json")
    monkeypatch.setattr(dea, "LATEST_JSON", memory / "daily_exam_latest.json")
    monkeypatch.setattr(dea, "HISTORY_JSONL", memory / "daily_exam_history.jsonl")
    monkeypatch.setattr(dea, "REPORT_MD", memory / "daily_exam_latest.md")
    monkeypatch.setattr(dea, "HEARTBEAT_PATH", state / "daily_exam_agent_heartbeat.json")
    monkeypatch.setattr(dea, "load_library", lambda: library)
    monkeypatch.setattr(dea, "safe_append_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(dea, "safe_append_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(dea, "safe_upsert_heartbeat", lambda *args, **kwargs: None)

    result = dea.run_once(force=True, max_log_lines=100)

    assert result["contract"]["paper_only"] is True
    assert result["contract"]["can_place_live_orders"] is False
    assert result["answer"].get("can_trade_live") is not True
    assert dea.LATEST_JSON.exists()
    assert dea.HISTORY_JSONL.exists()
    assert dea.REPORT_MD.exists()
    assert dea.HEARTBEAT_PATH.exists()

def test_run_once_skips_second_exam_same_day(tmp_path: Path, monkeypatch):
    memory = tmp_path / "memory"
    latest = memory / "daily_exam_latest.json"
    today = dea.local_date_key()
    write_json(latest, {"local_date": today, "exam_type": "risk_gate_review", "quality_score": 50})
    monkeypatch.setattr(dea, "LATEST_JSON", latest)
    monkeypatch.setattr(dea, "HEARTBEAT_PATH", tmp_path / "hb.json")
    monkeypatch.setattr(dea, "safe_upsert_heartbeat", lambda *args, **kwargs: None)

    result = dea.run_once(force=False)

    assert result["exam_type"] == "risk_gate_review"
    assert json.loads(dea.HEARTBEAT_PATH.read_text(encoding="utf-8"))["skipped"] is True
