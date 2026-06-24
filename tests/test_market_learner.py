from pathlib import Path

import market_learner as ml


def sample_snapshot() -> dict:
    return {
        "ts": "2026-06-20T00:00:00+00:00",
        "majors": [
            {"symbol": "BTCUSDT", "change_pct": 1.5, "range_pos": 0.85},
            {"symbol": "ETHUSDT", "change_pct": 1.7, "range_pos": 0.86},
            {"symbol": "SOLUSDT", "change_pct": 4.5, "range_pos": 0.84},
            {"symbol": "BNBUSDT", "change_pct": 1.2, "range_pos": 0.88},
        ],
        "hot": [
            {"symbol": "BTWUSDT", "change_pct": 91.0, "range_pos": 0.95},
            {"symbol": "BICOUSDT", "change_pct": 83.0, "range_pos": 0.70},
            {"symbol": "REUSDT", "change_pct": 80.0, "range_pos": 0.80},
            {"symbol": "EPICUSDT", "change_pct": -29.0, "range_pos": 0.04},
        ],
        "funding_extremes": [
            {"symbol": "REUSDT", "change_pct": 80.0, "funding_pct": -0.43},
            {"symbol": "BICOUSDT", "change_pct": 83.0, "funding_pct": -0.36},
            {"symbol": "BTWUSDT", "change_pct": 91.0, "funding_pct": 0.23},
        ],
    }


def test_classify_market_detects_mania_and_crowding():
    state = ml.classify_market(sample_snapshot())

    assert state["primary_regime"] == "risk_on"
    assert "alt_mania" in state["tags"]
    assert "crowded_funding" in state["tags"]
    assert state["recommended_min_signal_score"] == 8
    assert "REUSDT" in state["blocked_symbols"]


def test_trade_outcome_learning_blocks_bad_pair():
    events = [
        {"event": "paper_close", "symbol": "HYPEUSDT", "side": "SHORT", "net": "-0.04"},
        {"event": "paper_close", "symbol": "HYPEUSDT", "side": "SHORT", "net": "-0.03"},
    ]
    state = ml.classify_market(sample_snapshot())
    outcomes = ml.summarize_trade_outcomes(events)
    rules = ml.derive_learning_rules(state, outcomes)

    assert "HYPEUSDT" in rules["blocked_symbols"]
    assert rules["min_signal_score"] >= 8
    assert any("HYPEUSDT:SHORT" in rule for rule in rules["rules"])


def test_update_market_model_writes_model_and_report(tmp_path: Path):
    model_path = tmp_path / "market_model.json"
    report_path = tmp_path / "market_learning_latest.md"

    model = ml.update_market_model(sample_snapshot(), [], model_path=model_path, report_path=report_path)

    assert model["cycles"] == 1
    assert model["last_market_state"]["primary_regime"] == "risk_on"
    assert model_path.exists()
    assert "Market Learning" in report_path.read_text(encoding="utf-8")
