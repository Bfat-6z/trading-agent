"""Seed candidate methods for the Method Lab — a starter set encoding COMMON
shared TA approaches (RSI mean-reversion, EMA trend-following, breakouts, pullback
-in-trend, oversold-reversion, etc). These are CANDIDATES TO FALSIFY, not blessed
rules — run_lab decides which survive on real data. New methods (LLM-researched
from how others trade) get appended here in the same DSL, so the set grows without
any of them being trusted until the data says so.

DSL: when = list of {feat, op, val}; feats come from method_lab.feature_frame:
  rsi14, px_vs_ema20, px_vs_ema50, px_vs_ema200, ema_stack (-1/0/1),
  vol_ratio, ret5, ret20  (all values as of that bar's close).
"""
from __future__ import annotations

SEED_METHODS = [
    # --- mean-reversion on RSI ---
    {"id": "rsi_oversold_bounce", "name": "RSI oversold bounce",
     "desc": "Buy dips: RSI<30 (classic oversold long)", "side": "LONG",
     "when": [{"feat": "rsi14", "op": "<", "val": 30}], "sl_pct": 1.5, "tp_pct": 2.5},
    {"id": "rsi_overbought_fade", "name": "RSI overbought fade",
     "desc": "Short tops: RSI>70", "side": "SHORT",
     "when": [{"feat": "rsi14", "op": ">", "val": 70}], "sl_pct": 1.5, "tp_pct": 2.5},

    # --- RSI reversion WITH trend filter (only fade with the higher trend) ---
    {"id": "rsi_dip_in_uptrend", "name": "RSI dip in uptrend",
     "desc": "Buy RSI 35-45 dip while price above EMA200 (pullback in uptrend)",
     "side": "LONG", "when": [{"feat": "rsi14", "op": "<", "val": 45},
                              {"feat": "rsi14", "op": ">", "val": 32},
                              {"feat": "px_vs_ema200", "op": ">", "val": 0}],
     "sl_pct": 1.5, "tp_pct": 3.0},
    {"id": "rsi_pop_in_downtrend", "name": "RSI pop in downtrend",
     "desc": "Short RSI 55-65 bounce while price below EMA200", "side": "SHORT",
     "when": [{"feat": "rsi14", "op": ">", "val": 55}, {"feat": "rsi14", "op": "<", "val": 68},
              {"feat": "px_vs_ema200", "op": "<", "val": 0}], "sl_pct": 1.5, "tp_pct": 3.0},

    # --- trend-following on EMA stack ---
    {"id": "ema_stack_long", "name": "EMA stack trend long",
     "desc": "Long when close>EMA20>EMA50 (clean bull stack) + volume",
     "side": "LONG", "when": [{"feat": "ema_stack", "op": "==", "val": 1},
                              {"feat": "vol_ratio", "op": ">", "val": 1.2}],
     "sl_pct": 2.0, "tp_pct": 3.0},
    {"id": "ema_stack_short", "name": "EMA stack trend short",
     "desc": "Short when close<EMA20<EMA50 + volume", "side": "SHORT",
     "when": [{"feat": "ema_stack", "op": "==", "val": -1}, {"feat": "vol_ratio", "op": ">", "val": 1.2}],
     "sl_pct": 2.0, "tp_pct": 3.0},

    # --- momentum breakout ---
    {"id": "momo_breakout_long", "name": "Momentum breakout long",
     "desc": "Long strong 20-bar momentum + volume expansion", "side": "LONG",
     "when": [{"feat": "ret20", "op": ">", "val": 3.0}, {"feat": "vol_ratio", "op": ">", "val": 1.5},
              {"feat": "rsi14", "op": "<", "val": 72}], "sl_pct": 2.0, "tp_pct": 4.0},
    {"id": "momo_breakdown_short", "name": "Momentum breakdown short",
     "desc": "Short strong 20-bar downside momentum + volume", "side": "SHORT",
     "when": [{"feat": "ret20", "op": "<", "val": -3.0}, {"feat": "vol_ratio", "op": ">", "val": 1.5},
              {"feat": "rsi14", "op": ">", "val": 28}], "sl_pct": 2.0, "tp_pct": 4.0},

    # --- deep oversold reversion (capitulation) ---
    {"id": "capitulation_long", "name": "Capitulation reversion",
     "desc": "Long deep RSI<22 + high volume flush (mean-revert the panic)",
     "side": "LONG", "when": [{"feat": "rsi14", "op": "<", "val": 22},
                              {"feat": "vol_ratio", "op": ">", "val": 1.8}],
     "sl_pct": 2.5, "tp_pct": 4.0},

    # --- trend pullback to EMA20 (buy the dip to the fast EMA in an uptrend) ---
    {"id": "ema20_pullback_long", "name": "EMA20 pullback long",
     "desc": "Long when price dips to/below EMA20 but stack still bullish (EMA50<price)",
     "side": "LONG", "when": [{"feat": "px_vs_ema20", "op": "<", "val": 0.2},
                              {"feat": "px_vs_ema50", "op": ">", "val": 0},
                              {"feat": "px_vs_ema200", "op": ">", "val": 0}],
     "sl_pct": 1.8, "tp_pct": 3.0},
]
