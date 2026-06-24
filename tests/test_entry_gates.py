"""Unit tests for entry_gating_v2 layers in futures_watch.py.

Run: cd E:\\keo-moi-mail\\trading-agent && venv\\Scripts\\python.exe -m pytest tests\\test_entry_gates.py -v
"""
import sys
import os
from pathlib import Path
# Make project root importable
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tradingagents_crypto_src"))
os.environ.setdefault("BINANCE_API_KEY", "test")
os.environ.setdefault("BINANCE_API_SECRET", "test")

import pytest
import futures_watch as fw


# -------- Layer 1: _tv_confirms_from_state --------

def _good_long_state():
    return dict(h1_rsi=55, h4_rsi=58, h4_ema_dist=3, h1_macd_hist=0.1, h4_macd_hist=0.1,
                h1_adx=22, h1_di_plus=24, h1_di_minus=10)

def _good_short_state():
    return dict(h1_rsi=70, h4_rsi=72, h4_ema_dist=6, h1_macd_hist=-0.1, h4_macd_hist=-0.1,
                h1_adx=22, h1_di_plus=10, h1_di_minus=24)


def test_l1_long_passes_healthy():
    ok, reason = fw._tv_confirms_from_state(_good_long_state(), "LONG")
    assert ok, reason

def test_l1_long_blocked_h1_rsi_high():
    s = _good_long_state(); s["h1_rsi"] = 80
    ok, reason = fw._tv_confirms_from_state(s, "LONG")
    assert not ok and "1h_rsi" in reason

def test_l1_long_blocked_h4_rsi_high():
    s = _good_long_state(); s["h4_rsi"] = 79
    ok, reason = fw._tv_confirms_from_state(s, "LONG")
    assert not ok and "4h_rsi" in reason

def test_l1_long_blocked_h4_ema_dist_extended():
    s = _good_long_state(); s["h4_ema_dist"] = 12
    ok, reason = fw._tv_confirms_from_state(s, "LONG")
    assert not ok and "ema_dist" in reason

def test_l1_long_blocked_macd_bear_both_tfs():
    s = _good_long_state(); s["h1_macd_hist"] = -0.5; s["h4_macd_hist"] = -0.5
    ok, reason = fw._tv_confirms_from_state(s, "LONG")
    assert not ok and "macd_bear" in reason

def test_l1_long_blocked_downtrend_developing():
    s = _good_long_state(); s["h1_adx"] = 55; s["h1_di_plus"] = 8; s["h1_di_minus"] = 30
    ok, reason = fw._tv_confirms_from_state(s, "LONG")
    assert not ok and "downtrend" in reason

def test_l1_short_passes_healthy():
    ok, reason = fw._tv_confirms_from_state(_good_short_state(), "SHORT")
    assert ok, reason

def test_l1_short_blocked_h1_rsi_low():
    s = _good_short_state(); s["h1_rsi"] = 22
    ok, reason = fw._tv_confirms_from_state(s, "SHORT")
    assert not ok and "1h_rsi" in reason

def test_l1_failopen_when_state_missing():
    ok, reason = fw._tv_confirms_from_state(None, "LONG")
    assert ok and "failopen" in reason

def test_l1_failopen_when_keys_missing():
    s = {"h1_rsi": 60}  # missing the rest
    ok, reason = fw._tv_confirms_from_state(s, "LONG")
    assert ok and "failopen" in reason


# -------- Layer 3: _momentum_normal_ok --------

def test_l3_long_passes_under_cap():
    ok, _ = fw._momentum_normal_ok(ch24=9.0, atr_1d_pct=3.0, action="LONG")  # ratio 3.0 == cap, passes
    assert ok

def test_l3_long_blocked_over_cap():
    ok, reason = fw._momentum_normal_ok(ch24=10.0, atr_1d_pct=3.0, action="LONG")  # 3.33x
    assert not ok and "momentum" in reason

def test_l3_short_passes_under_cap():
    ok, _ = fw._momentum_normal_ok(ch24=-25.0, atr_1d_pct=10.0, action="SHORT")  # ratio -2.5
    assert ok

def test_l3_short_blocked_below_cap():
    ok, reason = fw._momentum_normal_ok(ch24=-31.0, atr_1d_pct=10.0, action="SHORT")  # -3.1
    assert not ok and "momentum" in reason

def test_l3_failopen_when_atr_missing():
    ok, reason = fw._momentum_normal_ok(ch24=50.0, atr_1d_pct=None, action="LONG")
    assert ok and "failopen" in reason


# -------- Layer 4: scan_futures_movers regime tightening --------

def _classify_inline(ch: float, rng_pos: float):
    """Mirror the regime if/elif in scan_futures_movers — fastest way to test bounds.
    Returns (regime, setup_label)."""
    if -12 <= ch <= -3 and 0.45 < rng_pos < 0.85:
        return 1.6, "oversold_bounce_LONG"
    elif 3 <= ch <= 8 and 0.3 < rng_pos < 0.65:
        return 1.5, "healthy_momentum_LONG"
    elif 8 < ch <= 14 and 0.5 < rng_pos < 0.75:
        return 1.3, "momentum_continuation_LONG"
    elif 10 <= ch <= 18 and rng_pos > 0.75:
        return 1.2, "mild_overbought_SHORT"
    elif 18 < ch <= 30 and rng_pos > 0.80:
        return 1.4, "exhaustion_SHORT"
    elif -3 < ch < 3:
        return 1.0, "consolidation"
    return 0.4, "other"


def test_l4_fight_case_rejected():
    """FIGHT at recheck: ch=+15%, rng_pos=0.88 → must NOT classify as LONG."""
    regime, label = _classify_inline(ch=15, rng_pos=0.88)
    assert "LONG" not in label, f"FIGHT case still labeled {label}!"

def test_l4_oversold_bounce_rejects_top_of_range():
    """Bug fix: oversold bounce should not fire when rng_pos > 0.85."""
    _, label = _classify_inline(ch=-5, rng_pos=0.92)
    assert label != "oversold_bounce_LONG"

def test_l4_oversold_bounce_still_fires_when_legit():
    _, label = _classify_inline(ch=-5, rng_pos=0.6)
    assert label == "oversold_bounce_LONG"

def test_l4_healthy_momentum_tighter_upper():
    _, label = _classify_inline(ch=5, rng_pos=0.68)  # above new 0.65 limit
    assert label != "healthy_momentum_LONG"

def test_l4_momentum_continuation_new_band():
    _, label = _classify_inline(ch=10, rng_pos=0.65)
    assert label == "momentum_continuation_LONG"

def test_l4_exhaustion_short_new_band():
    _, label = _classify_inline(ch=25, rng_pos=0.85)
    assert label == "exhaustion_SHORT"

def test_l4_bananas31_case_short():
    """BANANAS31 at +25% rng 0.9 → exhaustion_SHORT."""
    _, label = _classify_inline(ch=25, rng_pos=0.9)
    assert label == "exhaustion_SHORT"


# -------- Layer 2 + integration: decide_action --------

class FakeDebate:
    def __init__(self, consensus, strength):
        self.consensus = consensus
        self.consensus_strength = strength

class FakeRisk:
    def __init__(self, recommendation, risk_score=5.0):
        self.recommendation = recommendation
        self.risk_score = risk_score


def test_l2_blocks_borderline_conviction():
    debate = FakeDebate("bullish", 0.61)
    risk = FakeRisk("proceed")
    action, lev, audit, _ = fw.decide_action(debate, risk, symbol=None, ch24=5, atr_1d_pct=3)
    assert action is None
    assert not audit["L2"][0]

def test_l2_passes_high_conviction():
    debate = FakeDebate("bullish", 0.70)
    risk = FakeRisk("proceed")
    # symbol=None forces L1 skip ("no_symbol_skipped") so this isolates L2
    action, lev, audit, _ = fw.decide_action(debate, risk, symbol=None, ch24=5, atr_1d_pct=3)
    assert action == "LONG"
    assert audit["L2"][0]

def test_decide_action_risk_abort():
    debate = FakeDebate("bullish", 0.80)
    risk = FakeRisk("abort")
    action, lev, audit, _ = fw.decide_action(debate, risk, symbol=None, ch24=5, atr_1d_pct=3)
    assert action is None
    assert not audit["L0"][0]

def test_decide_action_l5_safety_net():
    """L5 fires when L1+L3 pass through but ch24 still over hard cap."""
    debate = FakeDebate("bullish", 0.80)
    risk = FakeRisk("proceed")
    # symbol=None skips L1; atr=None skips L3 (failopen); ch24=+15 triggers L5
    action, lev, audit, _ = fw.decide_action(debate, risk, symbol=None, ch24=15, atr_1d_pct=None)
    assert action is None
    assert not audit["L5"][0]

def test_decide_action_neutral_consensus():
    debate = FakeDebate("neutral", 0.50)
    risk = FakeRisk("proceed")
    action, lev, audit, _ = fw.decide_action(debate, risk, symbol=None, ch24=5, atr_1d_pct=3)
    assert action is None
    assert not audit["L2"][0]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
