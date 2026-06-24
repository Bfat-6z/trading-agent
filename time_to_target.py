"""Estimate time-to-target for an open position using:
1. Historical volatility (ATR-based random walk probability)
2. Historical pattern frequency (how often does X% move happen in N hours)
3. Catalyst timing
4. Current momentum
"""
from dotenv import load_dotenv
load_dotenv()
import math
import sys
from tradingagents.binance.client import spot_client


def analyze(symbol, entry, tp, sl, mark_now=None):
    c = spot_client()
    if mark_now is None:
        t = c.futures_symbol_ticker(symbol=symbol)
        mark_now = float(t["price"])

    print(f"\n=== TIME-TO-TARGET ANALYSIS {symbol} ===\n")
    print(f"Entry: ${entry}  Current: ${mark_now}  TP: ${tp}  SL: ${sl}")
    tp_pct = (tp - mark_now) / mark_now * 100
    sl_pct = (sl - mark_now) / mark_now * 100
    print(f"Distance to TP: {tp_pct:+.2f}%   Distance to SL: {sl_pct:+.2f}%\n")

    # === 1. Volatility math ===
    # Pull 4h klines for 30 days
    klines_4h = c.futures_klines(symbol=symbol, interval="4h", limit=180)  # 30 days
    if len(klines_4h) < 50:
        print(f"WARNING: only {len(klines_4h)} candles, may not be representative")

    # Compute 4h returns
    closes = [float(k[4]) for k in klines_4h]
    returns_4h = [(closes[i] - closes[i-1]) / closes[i-1] * 100 for i in range(1, len(closes))]
    pos_returns = [r for r in returns_4h if r > 0]
    neg_returns = [r for r in returns_4h if r < 0]
    avg_pos = sum(pos_returns) / len(pos_returns) if pos_returns else 0
    avg_neg = sum(neg_returns) / len(neg_returns) if neg_returns else 0
    stdev_4h = (sum((r - 0)**2 for r in returns_4h) / len(returns_4h))**0.5

    print(f"--- Volatility (last {len(returns_4h)} 4h candles) ---")
    print(f"  Avg positive 4h return: {avg_pos:+.2f}%")
    print(f"  Avg negative 4h return: {avg_neg:+.2f}%")
    print(f"  Stdev 4h return: {stdev_4h:.2f}%")
    print(f"  4h ATR equiv: ~{stdev_4h * 1.5:.2f}%")

    # Random walk hit probability (drift=0)
    # P(hit upper barrier before lower) = -lower / (upper - lower) for drift=0 random walk
    # Here barriers are tp_pct (upper) and sl_pct (lower, negative)
    p_tp = abs(sl_pct) / (abs(sl_pct) + tp_pct) if tp_pct > 0 else 0
    p_sl = tp_pct / (abs(sl_pct) + tp_pct) if tp_pct > 0 else 1
    print(f"\n  P(hit TP before SL) random walk = {p_tp*100:.0f}%")
    print(f"  P(hit SL before TP) random walk = {p_sl*100:.0f}%")

    # Expected time to hit (using stdev-based mixing time)
    # T ~ (TP_pct × SL_pct) / stdev^2 in 4h units
    t_to_hit_4h = (tp_pct * abs(sl_pct)) / (stdev_4h**2) if stdev_4h > 0 else 999
    print(f"  Expected time to hit either barrier: ~{t_to_hit_4h:.1f} candles = ~{t_to_hit_4h*4:.0f} hours = ~{t_to_hit_4h*4/24:.1f} days")

    # === 2. Pattern frequency ===
    print(f"\n--- Pattern: how often does +{tp_pct:.1f}% happen in N hours ---")
    # Look at every 24h window (6 candles) and check max return
    win = 6  # 6 × 4h = 24h
    hit_count = 0
    move_count = 0
    for i in range(len(closes) - win):
        start = closes[i]
        future = closes[i+1:i+win+1]
        max_up = max((f - start) / start * 100 for f in future)
        if max_up >= tp_pct:
            hit_count += 1
        move_count += 1
    if move_count > 0:
        p_24h = hit_count / move_count * 100
        print(f"  In 24h window: {p_24h:.0f}% of windows had +{tp_pct:.1f}% move ({hit_count}/{move_count})")

    win48 = 12  # 48h
    hit_count48 = 0; move_count48 = 0
    for i in range(len(closes) - win48):
        start = closes[i]
        future = closes[i+1:i+win48+1]
        max_up = max((f - start) / start * 100 for f in future)
        if max_up >= tp_pct:
            hit_count48 += 1
        move_count48 += 1
    if move_count48 > 0:
        p_48h = hit_count48 / move_count48 * 100
        print(f"  In 48h window: {p_48h:.0f}% of windows had +{tp_pct:.1f}% move ({hit_count48}/{move_count48})")

    win72 = 18  # 72h
    hit_count72 = 0; move_count72 = 0
    for i in range(len(closes) - win72):
        start = closes[i]
        future = closes[i+1:i+win72+1]
        max_up = max((f - start) / start * 100 for f in future)
        if max_up >= tp_pct:
            hit_count72 += 1
        move_count72 += 1
    if move_count72 > 0:
        p_72h = hit_count72 / move_count72 * 100
        print(f"  In 72h window: {p_72h:.0f}% of windows had +{tp_pct:.1f}% move ({hit_count72}/{move_count72})")

    # === 3. Current momentum velocity ===
    print(f"\n--- Current momentum (last 6 4h candles) ---")
    recent = closes[-6:]
    starts = closes[-7]
    end = closes[-1]
    move_pct = (end - starts) / starts * 100
    print(f"  Net 24h price change: {move_pct:+.2f}%")
    upd = sum(1 for i in range(1, len(recent)) if recent[i] > recent[i-1])
    print(f"  Up 4h candles last 24h: {upd}/{len(recent)-1}")

    # Volatility burst (today's range vs avg)
    klines_today = c.futures_klines(symbol=symbol, interval="1h", limit=24)
    today_high = max(float(k[2]) for k in klines_today)
    today_low = min(float(k[3]) for k in klines_today)
    today_range = (today_high - today_low) / today_low * 100
    print(f"  Today's range: {today_range:.1f}% (high ${today_high} low ${today_low})")

    # === 4. Summary forecast ===
    print(f"\n--- FORECAST ---")
    if t_to_hit_4h < 6:
        print(f"  Expected hit time: < 1 day. High activity setup.")
    elif t_to_hit_4h < 24:
        print(f"  Expected hit time: 1-4 days. Standard scalp timing.")
    elif t_to_hit_4h < 60:
        print(f"  Expected hit time: 1-10 days. Patient hold needed.")
    else:
        print(f"  Expected hit time: >10 days. Setup may be too tight for the volatility.")

    print(f"\n  Composite probability TP hits:")
    print(f"   - Random walk: {p_tp*100:.0f}%")
    print(f"   - Within 24h historical: {p_24h:.0f}%")
    print(f"   - Within 48h historical: {p_48h:.0f}%")
    print(f"   - Within 72h historical: {p_72h:.0f}%")

    return {
        "p_tp_random": p_tp,
        "p_tp_24h": p_24h/100,
        "p_tp_48h": p_48h/100,
        "p_tp_72h": p_72h/100,
        "expected_4h_periods": t_to_hit_4h,
    }


if __name__ == "__main__":
    analyze("ASTERUSDT", 0.6985, 0.7544, 0.68)
