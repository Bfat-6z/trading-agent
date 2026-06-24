from decimal import Decimal

from shadow_trade_logger import build_shadow_open, evaluate_shadow_trade


def _signal(side="LONG"):
    return {"symbol": "TESTUSDT", "side": side, "score": 8, "price": 100.0}


def _plan():
    return {"margin_usdt": "1", "leverage": 10, "notional": "10", "confidence": 0.7, "entry_type": "MARKET_NOW"}


def test_build_shadow_open_marks_no_execution_and_risk_reward():
    shadow = build_shadow_open(
        _signal("LONG"),
        _plan(),
        entry=Decimal("100"),
        stop=Decimal("99"),
        take_profit=Decimal("102"),
        block_reason="memory_sleep",
        ts="2026-06-20T00:00:00+00:00",
    )

    assert shadow["shadow_id"].startswith("shadow_")
    assert shadow["status"] == "open"
    assert shadow["no_execution"] is True
    assert shadow["block_reason"] == "memory_sleep"
    assert shadow["risk_pct"] == 1.0
    assert shadow["reward_pct"] == 2.0


def test_evaluate_shadow_trade_closes_long_take_profit_after_fees():
    shadow = build_shadow_open(_signal("LONG"), _plan(), "100", "99", "102", "critic_block", ts="2026-06-20T00:00:00+00:00")

    closed = evaluate_shadow_trade(shadow, "102")

    assert closed["status"] == "closed"
    assert closed["close_reason"] == "tp"
    assert Decimal(closed["gross"]) == Decimal("0.20")
    assert Decimal(closed["fees"]) == Decimal("0.0100")
    assert Decimal(closed["net"]) == Decimal("0.1900")


def test_evaluate_shadow_trade_closes_short_stop_loss():
    shadow = build_shadow_open(_signal("SHORT"), _plan(), "100", "101", "98", "memory_sleep", ts="2026-06-20T00:00:00+00:00")

    closed = evaluate_shadow_trade(shadow, "101")

    assert closed["status"] == "closed"
    assert closed["close_reason"] == "sl"
    assert Decimal(closed["net"]) < 0
