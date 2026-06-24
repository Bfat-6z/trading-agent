from argparse import Namespace
from pathlib import Path

import dream_cycle as dc


def sample_snapshot() -> dict:
    return {
        "ts": "2026-06-20T00:00:00+00:00",
        "majors": [
            {"symbol": "BTCUSDT", "change_pct": 1.5, "range_pos": 0.86},
            {"symbol": "ETHUSDT", "change_pct": 1.5, "range_pos": 0.84},
            {"symbol": "SOLUSDT", "change_pct": 5.0, "range_pos": 0.93},
            {"symbol": "BNBUSDT", "change_pct": 2.0, "range_pos": 0.90},
        ],
        "hot": [
            {"symbol": "REUSDT", "change_pct": 95.0, "range_pos": 0.86, "funding_pct": -0.42, "quote_volume": 1_600_000_000},
            {"symbol": "BTWUSDT", "change_pct": 90.0, "range_pos": 0.96, "funding_pct": 0.21, "quote_volume": 250_000_000},
            {"symbol": "BICOUSDT", "change_pct": 86.0, "range_pos": 0.78, "funding_pct": -0.37, "quote_volume": 360_000_000},
            {"symbol": "UBUSDT", "change_pct": -30.0, "range_pos": 0.10, "funding_pct": 0.005, "quote_volume": 30_000_000},
        ],
        "funding_extremes": [
            {"symbol": "REUSDT", "change_pct": 95.0, "range_pos": 0.86, "funding_pct": -0.42, "quote_volume": 1_600_000_000},
            {"symbol": "BTWUSDT", "change_pct": 90.0, "range_pos": 0.96, "funding_pct": 0.21, "quote_volume": 250_000_000},
            {"symbol": "BICOUSDT", "change_pct": 86.0, "range_pos": 0.78, "funding_pct": -0.37, "quote_volume": 360_000_000},
        ],
        "top_volume": [],
    }


def test_simulate_market_finds_block_risk():
    bias = {"blocked_symbols": ["REUSDT"], "blocked_sides": ["LONG"], "min_signal_score": 7}

    cycle = dc.simulate_market(sample_snapshot(), bias, limit=4)

    assert cycle["market_state"]["primary_regime"] == "risk_on"
    assert any(sim["verdict"] == "block" for sim in cycle["simulations"])
    assert any(sim["symbol"] == "REUSDT" for sim in cycle["blocks"])


def test_tighten_bias_never_lowers_controls():
    bias = {"blocked_symbols": ["REUSDT"], "blocked_sides": ["LONG"], "min_signal_score": 8, "sleep_until": "later"}
    patch = {"blocked_symbols": ["BTWUSDT"], "blocked_sides": [], "min_signal_score": 7, "high_risk_count": 3, "paper_candidates": []}

    tightened = dc.tighten_bias(bias, patch, "now")

    assert tightened["min_signal_score"] == 8
    assert tightened["sleep_until"] == "later"
    assert tightened["blocked_symbols"][:2] == ["REUSDT", "BTWUSDT"]
    assert tightened["blocked_sides"] == ["LONG"]


def test_run_once_writes_outputs_and_bias_patch(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(dc, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(dc, "MARKET_LATEST", tmp_path / "market.json")
    monkeypatch.setattr(dc, "BIAS_PATH", tmp_path / "bias.json")
    monkeypatch.setattr(dc, "DREAMS_MD", tmp_path / "DREAMS.md")
    monkeypatch.setattr(dc, "DREAM_LATEST_JSON", tmp_path / "dream_cycle_latest.json")
    monkeypatch.setattr(dc, "DREAM_CANDIDATES_JSONL", tmp_path / "dream_candidates.jsonl")
    monkeypatch.setattr(dc, "SIMULATION_RESULTS_JSONL", tmp_path / "simulation_results.jsonl")
    monkeypatch.setattr(dc, "HEARTBEAT_PATH", tmp_path / "heartbeat.json")
    monkeypatch.setattr(dc, "safe_append_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(dc, "safe_append_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(dc, "safe_upsert_heartbeat", lambda *args, **kwargs: None)
    dc.write_json(dc.MARKET_LATEST, sample_snapshot())
    dc.write_json(dc.BIAS_PATH, {"blocked_symbols": ["REUSDT"], "blocked_sides": ["LONG"], "min_signal_score": 8})

    result = dc.run_once(apply_bias=True, limit=4)

    assert result["bias_patch"]["high_risk_count"] >= 1
    assert dc.DREAMS_MD.exists()
    assert dc.DREAM_LATEST_JSON.exists()
    assert dc.SIMULATION_RESULTS_JSONL.exists()
    assert "dream_learning" in dc.read_json(dc.BIAS_PATH)

def test_run_loop_exits_when_existing_dream_cycle_is_running(tmp_path: Path, monkeypatch):
    pid_file = tmp_path / "dream.pid"
    pid_file.write_text("123", encoding="ascii")
    called = []

    monkeypatch.setattr(dc, "MEMORY_DIR", tmp_path)
    monkeypatch.setattr(dc, "PID_FILE", pid_file)
    monkeypatch.setattr(dc.os, "getpid", lambda: 999)
    monkeypatch.setattr(dc, "is_pid_running", lambda pid, expected_script=None: True)
    monkeypatch.setattr(dc, "run_once", lambda *args, **kwargs: called.append(True) or {})

    result = dc.run_loop(Namespace(once=False, no_apply_bias=False, limit=4, interval_minutes=30))

    assert result == 0
    assert called == []
    assert pid_file.read_text(encoding="ascii") == "123"
