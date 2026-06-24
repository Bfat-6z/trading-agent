from argparse import Namespace
import json
from pathlib import Path

import cognitive_supervisor as cs
import setup_skill_library as ssl


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_summarize_recent_paper_detects_latest_loss():
    rows = [
        {"event": "paper_close", "net": "0.1"},
        {"event": "paper_close", "net": "-0.04", "symbol": "BTCUSDT"},
        {"event": "risk_block"},
    ]

    paper = cs.summarize_recent_paper(rows)

    assert paper["closed_window"] == 2
    assert paper["wins"] == 1
    assert paper["losses"] == 1
    assert paper["latest_loss"]["symbol"] == "BTCUSDT"
    assert paper["risk_blocks"] == 1


def test_choose_focus_prioritizes_recent_loss():
    focus = cs.choose_focus(
        {"latest_loss": {"event": "paper_close", "net": "-0.1"}, "losses": 1},
        [{"hypothesis_id": "h1", "confidence_prior": 0.9}],
        ssl.default_library(),
        {"bias_patch": {"high_risk_count": 20}},
        {},
    )

    assert focus["focus_type"] == "confusing_loss"


def test_choose_focus_uses_highest_hypothesis_when_no_loss_or_dream_risk():
    focus = cs.choose_focus(
        {"latest_loss": None, "losses": 0},
        [
            {"hypothesis_id": "low", "setup_id": "a", "symbols": ["A"], "statement": "low", "confidence_prior": 0.2},
            {"hypothesis_id": "high", "setup_id": "b", "symbols": ["B"], "statement": "high", "confidence_prior": 0.8},
        ],
        ssl.default_library(),
        {"bias_patch": {"high_risk_count": 0}},
        {},
    )

    assert focus["focus_type"] == "hypothesis_test"
    assert focus["hypothesis_id"] == "high"


def test_propose_bias_never_lowers_controls():
    proposal = cs.propose_bias(
        {"min_signal_score": 8, "blocked_symbols": ["REUSDT"], "blocked_sides": ["LONG"]},
        {"focus_type": "dream_high_risk"},
        {"losses": 0},
        {"bias_patch": {"blocked_symbols": ["BTWUSDT"], "blocked_sides": []}},
    )

    assert proposal["min_signal_score"] == 8
    assert proposal["blocked_symbols"][:2] == ["REUSDT", "BTWUSDT"]
    assert proposal["blocked_sides"] == ["LONG"]
    assert proposal["can_loosen"] is False


def test_run_once_writes_cognitive_state(tmp_path: Path, monkeypatch):
    state = tmp_path / "state"
    memory = state / "agent_memory"
    monkeypatch.setattr(cs, "STATE_DIR", state)
    monkeypatch.setattr(cs, "MEMORY_DIR", memory)
    monkeypatch.setattr(cs, "MARKET_LATEST", state / "market_updates_latest.json")
    monkeypatch.setattr(cs, "MARKET_MODEL_PATH", memory / "market_model.json")
    monkeypatch.setattr(cs, "BIAS_PATH", memory / "execution_bias.json")
    monkeypatch.setattr(cs, "DREAM_LATEST", memory / "dream_cycle_latest.json")
    monkeypatch.setattr(cs, "HYPOTHESES_LATEST", memory / "hypotheses_latest.json")
    monkeypatch.setattr(cs, "MANUAL_THESES_PATH", memory / "manual_theses.jsonl")
    monkeypatch.setattr(cs, "SEMANTIC_MEMORY_PATH", memory / "semantic_memory.json")
    monkeypatch.setattr(cs, "SCALP_LOG", state / "scalp_autotrader.jsonl")
    monkeypatch.setattr(cs, "COGNITIVE_LATEST", memory / "cognitive_state_latest.json")
    monkeypatch.setattr(cs, "COGNITIVE_HISTORY", memory / "cognitive_state_history.jsonl")
    monkeypatch.setattr(cs, "COGNITIVE_REPORT", memory / "cognitive_state_latest.md")
    monkeypatch.setattr(cs, "HEARTBEAT_PATH", state / "cognitive_supervisor_heartbeat.json")
    monkeypatch.setattr(cs, "safe_append_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(cs, "safe_append_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(cs, "safe_upsert_heartbeat", lambda *args, **kwargs: None)
    monkeypatch.setattr(cs, "save_trace", lambda trace: trace)

    write_json(memory / "execution_bias.json", {"min_signal_score": 7, "blocked_symbols": [], "blocked_sides": []})
    write_json(memory / "dream_cycle_latest.json", {"bias_patch": {"high_risk_count": 9, "blocked_symbols": ["REUSDT"], "blocked_sides": ["LONG"]}})
    write_json(memory / "hypotheses_latest.json", {"hypotheses": [{"hypothesis_id": "h1", "setup_id": "funding_squeeze", "symbols": ["REUSDT"], "statement": "test", "confidence_prior": 0.7, "metrics": ["tp_before_sl"], "invalidation": ["bad"]}]})
    write_json(memory / "semantic_memory.json", {"latest": {"event_count": 4, "risk_blocks": {"memory_sleep": 1}}})
    (state / "scalp_autotrader.jsonl").parent.mkdir(parents=True, exist_ok=True)
    (state / "scalp_autotrader.jsonl").write_text('{"event":"signal"}\n', encoding="utf-8")

    result = cs.run_once()

    assert result["focus"]["focus_type"] == "dream_high_risk"
    assert result["bias_proposal"]["min_signal_score"] == 8
    assert result["reasoning_trace"]["thought_quality_score"] > 0
    assert result["reasoning_trace"]["decision"]["mode"] in {"paper_scan_with_shadow_logging", "paper_scan_allowed", "resolve_contradictions_first"}
    assert cs.COGNITIVE_LATEST.exists()
    assert cs.COGNITIVE_HISTORY.exists()
    assert cs.COGNITIVE_REPORT.exists()
    assert cs.HEARTBEAT_PATH.exists()

def test_run_loop_exits_when_existing_cognitive_supervisor_is_running(tmp_path: Path, monkeypatch):
    pid_file = tmp_path / "cognitive.pid"
    pid_file.write_text("123", encoding="ascii")
    called = []

    monkeypatch.setattr(cs, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(cs, "PID_FILE", pid_file)
    monkeypatch.setattr(cs.os, "getpid", lambda: 999)
    monkeypatch.setattr(cs, "is_pid_running", lambda pid, expected_script=None: True)
    monkeypatch.setattr(cs, "run_once", lambda: called.append(True) or {})

    result = cs.run_loop(Namespace(once=False, interval_minutes=20))

    assert result == 0
    assert called == []
    assert pid_file.read_text(encoding="ascii") == "123"
