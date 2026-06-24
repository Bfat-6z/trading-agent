from pathlib import Path

import hypothesis_engine as he
import setup_skill_library as ssl
import belief_ledger as bl


def sample_snapshot() -> dict:
    return {
        "ts": "2026-06-20T00:00:00+00:00",
        "majors": [
            {"symbol": "BTCUSDT", "change_pct": 2.0, "range_pos": 0.82},
            {"symbol": "ETHUSDT", "change_pct": 2.2, "range_pos": 0.84},
            {"symbol": "SOLUSDT", "change_pct": 3.0, "range_pos": 0.78},
        ],
        "hot": [
            {"symbol": "HYPEUSDT", "change_pct": 55.0, "range_pos": 0.94, "funding_pct": 0.22},
            {"symbol": "SOLUSDT", "change_pct": 8.0, "range_pos": 0.62, "funding_pct": 0.02},
            {"symbol": "REUSDT", "change_pct": 35.0, "range_pos": 0.88, "funding_pct": -0.21},
            {"symbol": "BICOUSDT", "change_pct": 31.0, "range_pos": 0.86, "funding_pct": -0.18},
            {"symbol": "UBUSDT", "change_pct": -28.0, "range_pos": 0.06, "funding_pct": 0.01},
        ],
        "funding_extremes": [
            {"symbol": "HYPEUSDT", "change_pct": 45.0, "range_pos": 0.94, "funding_pct": 0.22},
            {"symbol": "REUSDT", "change_pct": 35.0, "range_pos": 0.88, "funding_pct": -0.21},
            {"symbol": "BICOUSDT", "change_pct": 31.0, "range_pos": 0.86, "funding_pct": -0.18},
        ],
    }


def test_generate_market_hypotheses_for_risk_on_and_crowding():
    result = he.generate_hypotheses(
        sample_snapshot(),
        {},
        ssl.default_library(),
        bl.default_ledger(),
        {"blocked_symbols": []},
    )

    setup_ids = {item["setup_id"] for item in result["hypotheses"]}
    assert "momentum_continuation" in setup_ids
    assert "funding_squeeze" in setup_ids
    assert "exhaustion_fade" in setup_ids
    assert all(item["status"] == "testable" for item in result["hypotheses"])
    assert all(item["metrics"] for item in result["hypotheses"])
    assert all(item["invalidation"] for item in result["hypotheses"])


def test_manual_thesis_converts_chart_levels_to_hypothesis():
    hyp = he.manual_thesis_to_hypothesis(
        {
            "symbol": "BTCUSDT",
            "side": "LONG",
            "entry": 63683,
            "stop": 60728,
            "targets": [67255, 74427, 78059],
            "source": "whale_chart",
        }
    )

    assert hyp is not None
    assert hyp["setup_id"] == "manual_chart_thesis"
    assert hyp["symbols"] == ["BTCUSDT"]
    assert hyp["prediction"]["side"] == "LONG"
    assert hyp["prediction"]["rr_to_final"] > 4.0
    assert "price_closes_beyond_stop" in hyp["invalidation"]


def test_manual_thesis_missing_levels_is_ignored():
    assert he.manual_thesis_to_hypothesis({"symbol": "BTCUSDT", "side": "LONG"}) is None


def test_dedupes_hypotheses_for_same_inputs():
    manual = {
        "symbol": "BTCUSDT",
        "side": "LONG",
        "entry": 63683,
        "stop": 60728,
        "targets": [67255, 74427, 78059],
        "source": "whale_chart",
    }

    result = he.generate_hypotheses({}, {}, ssl.default_library(), bl.default_ledger(), {}, [manual, dict(manual)])

    manual_hyps = [item for item in result["hypotheses"] if item["source"] == "manual_thesis"]
    assert len(manual_hyps) == 1


def test_save_result_writes_latest_report_and_history(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(he, "REPORT_PATH", tmp_path / "hypotheses_latest.md")
    monkeypatch.setattr(he, "HYPOTHESES_HISTORY", tmp_path / "hypotheses_history.jsonl")
    monkeypatch.setattr(he, "safe_append_snapshot", lambda *args, **kwargs: None)
    monkeypatch.setattr(he, "safe_append_event", lambda *args, **kwargs: None)
    latest = tmp_path / "hypotheses_latest.json"
    result = he.generate_hypotheses(sample_snapshot(), {}, ssl.default_library(), bl.default_ledger(), {})

    he.save_result(result, latest_path=latest)

    assert latest.exists()
    assert he.REPORT_PATH.exists()
    assert he.HYPOTHESES_HISTORY.exists()
    assert "Hypotheses" in he.REPORT_PATH.read_text(encoding="utf-8")


def test_hypothesis_ids_are_stable():
    first = he.make_hypothesis("same", "setup", "risk_on", ["BTCUSDT"], {}, [])
    second = he.make_hypothesis("same", "setup", "risk_on", ["BTCUSDT"], {}, [])

    assert first["hypothesis_id"] == second["hypothesis_id"]
