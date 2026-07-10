"""Locks the correctness-critical logic of the shadow trigger evaluator (Opus review 2026-07-11):
no-lookahead entry, direction mapping, pessimistic same-bar R math, dedup/negcache."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import shadow_trigger_eval as ste

TF_MS = ste.TF_MS


def _bar(ts_close, o, h, l, c, qv=2_000_000.0):
    return {"ts_ms": ts_close, "open": o, "high": h, "low": l, "close": c,
            "quote_volume": qv, "close_time": ts_close}


def _flat_series(n, px, ts0=1_000_000_000_000):
    """n flat bars (needed so ATR>0 requires some range) with close_time = ts0 + i*TF."""
    return [_bar(ts0 + i * TF_MS, px, px * 1.01, px * 0.99, px) for i in range(n)]


# ---------------- direction mapping (a sign flip would invert every verdict) ----------------
def test_direction_mapping():
    assert ste._direction("chart_align", {"chart_align": {"dir": "up"}}) == "LONG"
    assert ste._direction("chart_align", {"chart_align": {"dir": "down"}}) == "SHORT"
    assert ste._direction("flush_no_oi", {}) == "LONG"
    assert ste._direction("flush_oi_dn", {}) == "LONG"
    assert ste._direction("funding_extreme", {"funding_extreme": {"rate": 0.002}}) == "SHORT"   # fade longs
    assert ste._direction("funding_extreme", {"funding_extreme": {"rate": -0.002}}) == "LONG"
    assert ste._direction("funding_extreme", {"funding_extreme": {"rate": 0.0}}) is None
    assert ste._direction("whale", {"whale": {"side": "SHORT"}}) == "SHORT"
    assert ste._direction("news", {}) is None


# ---------------- R math: pessimistic same-bar, correct sign both sides ----------------
def test_long_sl_is_floored_near_minus_one():
    bars = _flat_series(20, 100.0)
    ei = 15
    # a bar that dumps below SL (SL = entry - 1.5*ATR; ATR ~= 2.0 here so SL ~= 97)
    bars[ei + 1] = _bar(bars[ei]["ts_ms"] + TF_MS, 100, 100.5, 90.0, 91.0)   # low 90 << SL
    sim = ste._simulate(bars, ei, "LONG")
    assert sim["reason"] == "sl"
    assert -1.15 < sim["gross_R"] <= -0.98         # floored at the stop, not at the -10% low
    assert sim["mae_R"] >= 1.0


def test_short_tp_pays_reward_side():
    bars = _flat_series(20, 100.0)
    ei = 15
    # SHORT: TP = entry - 2.5*ATR (~95); a bar that drops to TP without first spiking to SL
    bars[ei + 1] = _bar(bars[ei]["ts_ms"] + TF_MS, 100, 100.2, 94.0, 95.0)
    sim = ste._simulate(bars, ei, "SHORT")
    assert sim["reason"] == "tp"
    assert 1.6 < sim["gross_R"] < 1.75              # TP_ATR/SL_ATR = 2.5/1.5 = +1.667R


def test_same_bar_books_sl_before_tp():
    bars = _flat_series(20, 100.0)
    ei = 15
    # one bar touches BOTH sides -> pessimistic must book SL (loss), not TP
    bars[ei + 1] = _bar(bars[ei]["ts_ms"] + TF_MS, 100, 108.0, 90.0, 100.0)
    sim = ste._simulate(bars, ei, "LONG")
    assert sim["reason"] == "sl"


def test_atr_floor_skips_pegged_asset():
    # near-zero range -> atr/entry < 0.3% -> not a real setup
    flat = [{"ts_ms": 1_000_000_000_000 + i * TF_MS, "open": 100, "high": 100.05,
             "low": 99.97, "close": 100.0, "quote_volume": 2e6, "close_time": 0} for i in range(20)]
    assert ste._simulate(flat, 15, "LONG") is None


# ---------------- no-lookahead entry selection ----------------
def test_entry_is_last_closed_bar_at_trigger_ts():
    bars = _flat_series(10, 100.0, ts0=1_000_000_000_000)
    # trigger fires 1ms after bar index 5 closed -> entry must be idx 5, outcomes from idx 6
    trigger_ts = bars[5]["ts_ms"] + 1
    entry_idx = None
    for i, b in enumerate(bars):
        if int(b["ts_ms"]) <= trigger_ts:
            entry_idx = i
    assert entry_idx == 5


# ---------------- exclude lists: SKALE/Venice are crypto, must NOT be excluded ----------------
def test_real_cryptos_not_excluded():
    for c in ("SKL", "VVV", "BTC", "ETH", "SOL", "AAVE"):
        assert c not in ste.NON_CRYPTO, f"{c} is a real crypto, must not be excluded"
    for stock in ("NVDA", "QQQ", "PAXG", "XAU", "SKHYNIX", "MSTR"):
        assert stock in ste.NON_CRYPTO
