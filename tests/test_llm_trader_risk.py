"""Tests for llm_trader_risk (plan 260702-0900, extraction items #1-#4).

Covers plan acceptance criteria exactly:
- #2: LONG x10 mmr 1% liquidates at -9.0%; bar touching both liq and sl
      resolves to "liquidation"; net == -margin on liquidation.
- #3: funding sign — LONG pays a positive rate, SHORT receives it.
- #4: can_open refuses at 4 open positions and at >60% total margin;
      daily_breaker blocks after a -15% UTC day.

Run: cd E:\\keo-moi-mail\\trading-agent && venv\\Scripts\\python.exe -m pytest tests\\test_llm_trader_risk.py -q
"""
import sys
from pathlib import Path

# Make project root importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

import llm_trader_risk as risk


# A UTC-day-aligned base timestamp (mid-2026) for breaker tests.
DAY_MS = 86_400_000
TODAY0 = (1_782_000_000_000 // DAY_MS) * DAY_MS
NOW = TODAY0 + 12 * 3_600_000  # midday UTC


# -------- mmr_for --------

def test_mmr_btc_eth_are_major():
    assert risk.mmr_for("BTCUSDT") == pytest.approx(0.005)
    assert risk.mmr_for("ETHUSDT") == pytest.approx(0.005)
    assert risk.mmr_for("ethusdt") == pytest.approx(0.005)


def test_mmr_alts_get_pessimistic_default():
    assert risk.mmr_for("SOLUSDT") == pytest.approx(0.01)
    assert risk.mmr_for("MAGMAUSDT") == pytest.approx(0.01)
    # Prefix trap: ETHFI is an alt, must NOT inherit the ETH major rate.
    assert risk.mmr_for("ETHFIUSDT") == pytest.approx(0.01)
    assert risk.mmr_for("") == pytest.approx(0.01)


# -------- liquidation_price (acceptance #2, math) --------

def test_liq_long_x10_mmr1pct_is_minus_9pct():
    """Acceptance #2: LONG x10 mmr 1% -> liquidation at -9.0% from entry."""
    entry = 100.0
    liq = risk.liquidation_price(entry, 10, "LONG", 0.01)
    assert liq == pytest.approx(91.0)
    assert (liq - entry) / entry == pytest.approx(-0.09)


def test_liq_short_x10_mmr1pct_is_plus_9pct():
    liq = risk.liquidation_price(100.0, 10, "SHORT", 0.01)
    assert liq == pytest.approx(109.0)


def test_liq_long_x5_btc_mmr():
    # x5 with mmr 0.5%: 1 - 0.2 + 0.005 = 0.805
    assert risk.liquidation_price(50_000.0, 5, "LONG", 0.005) == pytest.approx(40_250.0)


def test_liq_invalid_inputs_raise():
    with pytest.raises(ValueError):
        risk.liquidation_price(100.0, 10, "SIDEWAYS", 0.01)
    with pytest.raises(ValueError):
        risk.liquidation_price(100.0, 0, "LONG", 0.01)


# -------- exit_check (acceptance #2, pessimistic ordering) --------

def test_exit_bar_touching_liq_and_sl_is_liquidation():
    """Acceptance #2: bar touches BOTH liq (91) and sl (95) -> liquidation."""
    bar = {"high": 101.0, "low": 90.0}
    out = risk.exit_check(bar, "LONG", 91.0, 95.0, 110.0)
    assert out == (91.0, "liquidation")


def test_exit_long_sl_before_tp_when_both_touched():
    # Pessimistic: bar spans sl AND tp; assume sl traded first.
    bar = {"high": 111.0, "low": 94.0}
    out = risk.exit_check(bar, "LONG", 80.0, 95.0, 110.0)
    assert out == (95.0, "sl")


def test_exit_long_tp_only():
    bar = {"high": 111.0, "low": 99.0}
    out = risk.exit_check(bar, "LONG", 91.0, 95.0, 110.0)
    assert out == (110.0, "tp")


def test_exit_none_when_nothing_touched():
    bar = {"high": 102.0, "low": 98.0}
    assert risk.exit_check(bar, "LONG", 91.0, 95.0, 110.0) is None


def test_exit_short_liquidation_before_sl():
    # SHORT: adverse side is UP. Bar high 110 touches liq 109 and sl 105.
    bar = {"high": 110.0, "low": 100.0}
    out = risk.exit_check(bar, "SHORT", 109.0, 105.0, 90.0)
    assert out == (109.0, "liquidation")


def test_exit_short_sl_and_tp():
    assert risk.exit_check({"high": 106.0, "low": 96.0}, "SHORT", 109.0, 105.0, 90.0) == (105.0, "sl")
    assert risk.exit_check({"high": 101.0, "low": 89.0}, "SHORT", 109.0, 105.0, 90.0) == (90.0, "tp")


def test_exit_absent_levels_are_skipped():
    # sl=0 means "no stop set": must not fire a phantom sl at price 0.
    bar = {"high": 102.0, "low": 1.0}
    out = risk.exit_check(bar, "LONG", 0.0, 0.0, 110.0)
    assert out is None


# -------- funding_cost (acceptance #3, signs) --------

FUND_EVENTS = [(NOW + 1, 0.0001), (NOW + 2, 0.0001), (NOW + 3, 0.0001)]


def test_funding_long_pays_positive_rate():
    """Acceptance #3: LONG pays when rate > 0 -> positive total cost."""
    cost = risk.funding_cost("LONG", 10.0, 100.0, FUND_EVENTS, NOW, NOW + 10)
    assert cost == pytest.approx(0.3)  # 3 * 0.0001 * (10 * 100)
    assert cost > 0


def test_funding_short_receives_positive_rate():
    """Acceptance #3: SHORT receives the same events -> negative (income)."""
    cost = risk.funding_cost("SHORT", 10.0, 100.0, FUND_EVENTS, NOW, NOW + 10)
    assert cost == pytest.approx(-0.3)
    assert cost < 0


def test_funding_negative_rate_flips_both_sides():
    events = [(NOW + 1, -0.0002)]
    assert risk.funding_cost("LONG", 10.0, 100.0, events, NOW, NOW + 10) == pytest.approx(-0.2)
    assert risk.funding_cost("SHORT", 10.0, 100.0, events, NOW, NOW + 10) == pytest.approx(0.2)


def test_funding_window_is_half_open_t0_excluded_t1_included():
    events = [(NOW, 0.0001), (NOW + 5, 0.0001), (NOW + 10, 0.0001), (NOW + 11, 0.0001)]
    # (t0, t1] -> only NOW+5 and NOW+10 charge.
    cost = risk.funding_cost("LONG", 10.0, 100.0, events, NOW, NOW + 10)
    assert cost == pytest.approx(0.2)


def test_funding_empty_events_is_zero():
    assert risk.funding_cost("LONG", 10.0, 100.0, [], NOW, NOW + 10) == 0.0


# -------- trade_costs --------

def test_trade_costs_major_tier():
    out = risk.trade_costs(100.0, 110.0, 2.0, 600_000_000)
    assert out["tier"] == "major"
    # taker 5bps on both legs: 0.0005 * (100 + 110) * 2
    assert out["fee"] == pytest.approx(0.21)
    assert out["slip_bps"] == pytest.approx(3.0)  # 2 slip + 1 half-spread


def test_trade_costs_mid_and_micro_tiers():
    mid = risk.trade_costs(1.0, 1.1, 100.0, 100_000_000)
    assert mid["tier"] == "mid"
    assert mid["slip_bps"] == pytest.approx(16.0)  # 10 + 6
    micro = risk.trade_costs(1.0, 1.1, 100.0, 1_000_000)
    assert micro["tier"] == "micro"
    assert micro["slip_bps"] == pytest.approx(70.0)  # 40 + 30
    # Same notional, fees identical across tiers (fee is flat taker rate).
    assert micro["fee"] == pytest.approx(mid["fee"])


# -------- net_pnl (acceptance #2, -margin on liquidation) --------

def test_net_pnl_liquidation_is_exactly_minus_margin():
    """Acceptance #2: liquidated -> net == -margin, regardless of residuals."""
    net = risk.net_pnl("LONG", 100.0, 91.0, 1.0, 10.0, 0.5, 0.1, True)
    assert net == -10.0


def test_net_pnl_long_win():
    # gross (110-100)*2 = 20, minus fee 0.21 minus funding 0.3
    net = risk.net_pnl("LONG", 100.0, 110.0, 2.0, 20.0, 0.21, 0.3, False)
    assert net == pytest.approx(19.49)


def test_net_pnl_short_win_and_funding_income():
    # gross (100-90)*1 = 10, fee 0.1, funding -0.3 (received) -> 10.2
    net = risk.net_pnl("SHORT", 100.0, 90.0, 1.0, 20.0, 0.1, -0.3, False)
    assert net == pytest.approx(10.2)


def test_net_pnl_isolated_floor_without_liquidation_flag():
    # gross -11 on margin 10: isolated margin cannot lose more than posted.
    net = risk.net_pnl("LONG", 100.0, 89.0, 1.0, 10.0, 0.5, 0.0, False)
    assert net == -10.0


# -------- can_open (acceptance #4, caps) --------

def _pos(margin: float) -> dict:
    return {"symbol": "XUSDT", "side": "LONG", "margin": margin, "leverage": 5}


def test_can_open_refuses_at_4_concurrent():
    """Acceptance #4: 4 already open -> (False, reason)."""
    ok, reason = risk.can_open(5.0, 100.0, [_pos(5.0)] * 4)
    assert ok is False
    assert "max_concurrent" in reason


def test_can_open_refuses_over_60pct_total_margin():
    """Acceptance #4: existing 55 + new 10 = 65 > 60% of 100 -> refuse."""
    ok, reason = risk.can_open(10.0, 100.0, [_pos(30.0), _pos(25.0)])
    assert ok is False
    assert "margin" in reason


def test_can_open_allows_exactly_at_cap_and_below():
    ok, reason = risk.can_open(10.0, 100.0, [_pos(30.0), _pos(20.0)])  # == 60%
    assert ok is True and reason == "ok"
    assert risk.can_open(8.0, 100.0, [])[0] is True


def test_can_open_fail_closed_on_malformed_inputs():
    assert risk.can_open(10.0, 100.0, [{"no_margin_key": 1}])[0] is False
    assert risk.can_open(10.0, 0.0, [])[0] is False       # dead account
    assert risk.can_open(-5.0, 100.0, [])[0] is False     # nonsense margin
    assert risk.can_open("abc", 100.0, [])[0] is False    # non-numeric


# -------- daily_breaker (acceptance #4, -15% UTC day) --------

def _trade(net: float, ts: int) -> dict:
    return {"symbol": "XUSDT", "net": net, "closed_ts": ts, "r": -1.0}


def test_breaker_blocks_at_minus_15pct_day():
    """Acceptance #4: realized -15 on day-start equity 100 -> blocked."""
    closed = [_trade(-10.0, TODAY0 + 3_600_000), _trade(-5.0, TODAY0 + 7_200_000)]
    blocked, reason = risk.daily_breaker(closed, 100.0, NOW)
    assert blocked is True
    assert "daily_breaker" in reason


def test_breaker_open_just_above_threshold():
    closed = [_trade(-14.9, TODAY0 + 3_600_000)]
    blocked, reason = risk.daily_breaker(closed, 100.0, NOW)
    assert blocked is False and reason == "ok"


def test_breaker_ignores_other_utc_days():
    # Huge loss yesterday + late yesterday edge (1ms before midnight): ignored.
    closed = [_trade(-50.0, TODAY0 - DAY_MS + 3_600_000), _trade(-50.0, TODAY0 - 1)]
    assert risk.daily_breaker(closed, 100.0, NOW)[0] is False
    # Same rows but now_ms moved to yesterday -> they count -> blocked.
    assert risk.daily_breaker(closed, 100.0, TODAY0 - DAY_MS + 7_200_000)[0] is True


def test_breaker_malformed_rows_count_as_zero_not_unblock():
    closed = [
        {"net": "garbage", "closed_ts": TODAY0 + 1},   # bad net -> 0
        {"net": -16.0},                                 # missing closed_ts -> 0
        "not-even-a-dict",                              # -> 0
        _trade(-16.0, TODAY0 + 2),                      # valid loss still blocks
    ]
    blocked, _ = risk.daily_breaker(closed, 100.0, NOW)
    assert blocked is True


def test_breaker_fail_closed_on_exception():
    blocked, reason = risk.daily_breaker(None, 100.0, NOW)
    assert (blocked, reason) == (True, "breaker_error_fail_closed")
    blocked, reason = risk.daily_breaker([], None, NOW)
    assert (blocked, reason) == (True, "breaker_error_fail_closed")


def test_breaker_blocks_on_invalid_day_start_equity():
    assert risk.daily_breaker([], 0.0, NOW)[0] is True


# -------- regressions: NaN fail-open, gap-through fills, stop slippage --------
# NaN never raises and every comparison with NaN is False, so without explicit
# isfinite gates the fail-closed guards were silently bypassed (fail-OPEN).

NAN = float("nan")
INF = float("inf")


def test_can_open_fail_closed_on_nan_and_inf():
    # Regression: can_open(NaN, 100.0, []) used to return (True, "ok").
    assert risk.can_open(NAN, 100.0, [])[0] is False
    assert risk.can_open(10.0, NAN, [])[0] is False
    # NaN position margins poison the sum -> cap check always False -> allowed.
    assert risk.can_open(10.0, 100.0, [{"margin": NAN}] * 3)[0] is False
    # NaN/inf cap would approve any total margin.
    assert risk.can_open(10.0, 100.0, [], max_total_margin_pct=NAN)[0] is False
    assert risk.can_open(10.0, 100.0, [], max_total_margin_pct=INF)[0] is False
    assert risk.can_open(INF, 100.0, [])[0] is False


def test_breaker_blocks_on_nan_day_start_equity():
    # Regression: NaN eq0 passed the <=0 guard and produced a NaN limit that
    # never compared True -> (False, "ok") even with a real -50 day.
    closed = [_trade(-50.0, TODAY0 + 1)]
    assert risk.daily_breaker(closed, NAN, NOW)[0] is True


def test_breaker_nan_row_cannot_poison_real_losses():
    # Regression: one NaN net row made realized=NaN, permanently disabling the
    # breaker. NaN rows must contribute 0; the real -50 loss must still block.
    closed = [_trade(NAN, TODAY0 + 1), _trade(-50.0, TODAY0 + 2)]
    assert risk.daily_breaker(closed, 100.0, NOW)[0] is True


def test_breaker_blocks_on_nan_loss_pct():
    assert risk.daily_breaker([], 100.0, NOW, max_daily_loss_pct=NAN)[0] is True


def test_exit_check_raises_on_non_finite_inputs():
    # Regression: NaN liq/sl were treated as present-but-never-touched, so
    # liquidation and SL silently never fired while TP still could.
    bar = {"high": 100.0, "low": 50.0}
    with pytest.raises(ValueError):
        risk.exit_check(bar, "LONG", NAN, NAN, 110.0)
    with pytest.raises(ValueError):
        risk.exit_check(bar, "LONG", 91.0, NAN, 110.0)
    with pytest.raises(ValueError):
        risk.exit_check(bar, "LONG", 91.0, INF, 110.0)
    with pytest.raises(ValueError):
        risk.exit_check({"high": NAN, "low": 50.0}, "LONG", 91.0, 95.0, 110.0)
    with pytest.raises(ValueError):
        risk.exit_check({"high": 100.0, "low": NAN}, "LONG", 91.0, 95.0, 110.0)
    with pytest.raises(ValueError):
        risk.exit_check({"high": 100.0, "low": 50.0, "open": NAN}, "LONG", 91.0, 95.0, 110.0)


def test_exit_long_gap_through_sl_fills_at_open():
    # Regression: used to book (95.0, "sl") — a price that never traded.
    bar = {"open": 88.0, "high": 90.0, "low": 85.0}
    assert risk.exit_check(bar, "LONG", 0.0, 95.0, 110.0) == (88.0, "sl")


def test_exit_long_gap_without_open_clamps_to_bar_range():
    # No open available: fill still may not leave the traded range.
    bar = {"high": 90.0, "low": 85.0}
    assert risk.exit_check(bar, "LONG", 0.0, 95.0, 110.0) == (90.0, "sl")


def test_exit_short_gap_up_fills_at_open():
    bar = {"open": 112.0, "high": 115.0, "low": 110.0}
    assert risk.exit_check(bar, "SHORT", 0.0, 105.0, 90.0) == (112.0, "sl")


def test_exit_normal_touch_unaffected_by_open():
    # Non-gap bar: stop level inside the range still fills exactly at the stop.
    bar = {"open": 100.0, "high": 101.0, "low": 94.0}
    assert risk.exit_check(bar, "LONG", 80.0, 95.0, 110.0) == (95.0, "sl")


def test_exit_gap_through_liquidation_clamped_too():
    # Same clamp applies to the liquidation level (net is pinned to -margin
    # by net_pnl anyway, but the booked fill must be a traded price).
    bar = {"open": 86.0, "high": 88.0, "low": 84.0}
    assert risk.exit_check(bar, "LONG", 91.0, 95.0, 110.0) == (86.0, "liquidation")


def test_trade_costs_stop_slippage_applies_multiplier():
    # Regression: trade_costs exposed only non-stop slippage; SL/liquidation
    # exits (the loss path) understated adverse slippage ~2x.
    micro = risk.trade_costs(1.0, 1.1, 100.0, 1_000_000)
    assert micro["slip_bps_stop"] == pytest.approx(150.0)  # 40*3 + 30
    mid = risk.trade_costs(1.0, 1.1, 100.0, 100_000_000)
    assert mid["slip_bps_stop"] == pytest.approx(36.0)     # 10*3 + 6
    major = risk.trade_costs(100.0, 110.0, 2.0, 600_000_000)
    assert major["slip_bps_stop"] == pytest.approx(7.0)    # 2*3 + 1
    assert micro["slip_bps_stop"] > micro["slip_bps"]
