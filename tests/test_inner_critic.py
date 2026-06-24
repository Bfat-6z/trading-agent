from datetime import datetime, timedelta, timezone

from inner_critic import evaluate_signal
from setup_skill_library import default_library


def _signal(**overrides):
    payload = {
        "symbol": "TESTUSDT",
        "side": "LONG",
        "score": 8,
        "price": 100.0,
        "quote_volume_m": 200.0,
        "spread_pct": 0.01,
        "change_3m_pct": 0.3,
        "change_5m_pct": 0.5,
        "change_10m_pct": 0.8,
        "volume_ratio_1m": 1.4,
        "rsi_1m": 58.0,
        "taker_flow_last": 1.2,
        "taker_flow_avg": 1.0,
        "reasons": ["test"],
    }
    payload.update(overrides)
    return payload


def _snapshot():
    return {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "top_volume": [
            {
                "symbol": "TESTUSDT",
                "quote_volume": 500_000_000,
                "change_pct": 3.0,
                "range_pos": 0.55,
                "funding_pct": 0.01,
            }
        ]
    }


def _market_model():
    return {"last_market_state": {"tags": ["risk_on"], "primary_regime": "risk_on"}}


def _evaluate(signal, **overrides):
    kwargs = {
        "bias": {},
        "snapshot": {},
        "market_model": {},
        "library": default_library(),
        "hypotheses_result": {"hypotheses": []},
        "news_context": {},
    }
    kwargs.update(overrides)
    return evaluate_signal(signal, **kwargs)


def test_invalid_signal_blocks(monkeypatch):
    monkeypatch.setattr("inner_critic.safe_append_event", lambda *args, **kwargs: None)

    verdict = _evaluate({"symbol": "", "side": "LONG", "score": 9})

    assert verdict["verdict"] == "block"
    assert "invalid_signal" in verdict["reasons"]


def test_memory_sleep_blocks(monkeypatch):
    monkeypatch.setattr("inner_critic.safe_append_event", lambda *args, **kwargs: None)
    sleep_until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")

    verdict = _evaluate(_signal(), bias={"sleep_until": sleep_until})

    assert verdict["verdict"] == "block"
    assert "memory_sleep_active" in verdict["reasons"]


def test_blocked_symbol_and_side_block(monkeypatch):
    monkeypatch.setattr("inner_critic.safe_append_event", lambda *args, **kwargs: None)

    verdict = _evaluate(_signal(), bias={"blocked_symbols": ["TESTUSDT"], "blocked_sides": ["LONG"]})

    assert verdict["verdict"] == "block"
    assert "symbol_blocked_by_memory" in verdict["reasons"]
    assert "side_blocked_by_memory" in verdict["reasons"]


def test_score_below_memory_minimum_blocks(monkeypatch):
    monkeypatch.setattr("inner_critic.safe_append_event", lambda *args, **kwargs: None)

    verdict = _evaluate(_signal(score=7), bias={"min_signal_score": 8})

    assert verdict["verdict"] == "block"
    assert "score_below_memory_minimum" in verdict["reasons"]


def test_no_setup_match_blocks(monkeypatch):
    monkeypatch.setattr("inner_critic.safe_append_event", lambda *args, **kwargs: None)

    verdict = _evaluate(_signal(), bias={"min_signal_score": 6})

    assert verdict["verdict"] == "block"
    assert verdict["setup_ids"] == []
    assert "no_setup_match" in verdict["reasons"]


def test_stale_market_snapshot_blocks(monkeypatch):
    monkeypatch.setattr("inner_critic.safe_append_event", lambda *args, **kwargs: None)
    stale_ts = (datetime.now(timezone.utc) - timedelta(minutes=20)).isoformat(timespec="seconds")
    snapshot = _snapshot()
    snapshot["ts"] = stale_ts

    verdict = _evaluate(
        _signal(),
        snapshot=snapshot,
        market_model=_market_model(),
    )

    assert verdict["verdict"] == "block"
    assert "stale_market_snapshot" in verdict["reasons"]
    assert verdict["stale_data"][0]["status"] == "stale"


def test_setup_match_without_supporting_hypothesis_tightens(monkeypatch):
    monkeypatch.setattr("inner_critic.safe_append_event", lambda *args, **kwargs: None)

    verdict = _evaluate(
        _signal(),
        snapshot=_snapshot(),
        market_model=_market_model(),
    )

    assert verdict["verdict"] == "tighten"
    assert "momentum_continuation" in verdict["setup_ids"]
    assert "no_supporting_hypothesis" in verdict["reasons"]
    assert verdict["tighten_min_signal_score"] >= 7


def test_setup_match_with_supporting_hypothesis_allows_paper(monkeypatch):
    monkeypatch.setattr("inner_critic.safe_append_event", lambda *args, **kwargs: None)
    hypotheses = {
        "hypotheses": [
            {
                "hypothesis_id": "hyp-test-long",
                "symbols": ["TESTUSDT"],
                "setup_id": "momentum_continuation",
                "prediction": {"side": "LONG"},
            }
        ]
    }

    verdict = _evaluate(
        _signal(),
        snapshot=_snapshot(),
        market_model=_market_model(),
        hypotheses_result=hypotheses,
    )

    assert verdict["verdict"] == "allow_paper"
    assert verdict["reasons"] == ["critic_passed"]
    assert verdict["hypothesis_ids"] == ["hyp-test-long"]

def test_high_news_risk_blocks_before_setup(monkeypatch):
    monkeypatch.setattr("inner_critic.safe_append_event", lambda *args, **kwargs: None)
    now = datetime.now(timezone.utc)
    news = {
        "ts": now.isoformat(timespec="seconds"),
        "event_count": 4,
        "macro_risk_score": 0.7,
        "crypto_regulatory_risk": 0.8,
        "headline_chaos": 0.2,
        "symbol_impacts": {},
    }

    verdict = _evaluate(
        _signal(),
        snapshot=_snapshot(),
        market_model=_market_model(),
        news_context=news,
    )

    assert verdict["verdict"] == "block"
    assert "high_news_macro_or_regulatory_risk" in verdict["reasons"]
    assert verdict["news_context"]["can_loosen"] is False

def test_news_conflict_tightens_without_allowing_weak_entry(monkeypatch):
    monkeypatch.setattr("inner_critic.safe_append_event", lambda *args, **kwargs: None)
    now = datetime.now(timezone.utc)
    hypotheses = {
        "hypotheses": [
            {
                "hypothesis_id": "hyp-test-long",
                "symbols": ["TESTUSDT"],
                "setup_id": "momentum_continuation",
                "prediction": {"side": "LONG"},
            }
        ]
    }
    news = {
        "ts": now.isoformat(timespec="seconds"),
        "event_count": 2,
        "macro_risk_score": 0.1,
        "crypto_regulatory_risk": 0.1,
        "headline_chaos": 0.1,
        "symbol_impacts": {"TEST": {"risk": 0.3, "bearish": 0.3, "bullish": 0.0}},
    }

    verdict = _evaluate(
        _signal(),
        snapshot=_snapshot(),
        market_model=_market_model(),
        hypotheses_result=hypotheses,
        news_context=news,
    )

    assert verdict["verdict"] == "tighten"
    assert "news_conflicts_with_long" in verdict["reasons"]
    assert verdict["tighten_min_signal_score"] >= 7
