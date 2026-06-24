"""Solve trade parameters to hit exact NET profit target by time T.

The math:
  net_profit = qty × (TP - entry) - open_fee - close_fee - funding × cycles
  net_profit = X (target)
  =>  TP = entry + (X + total_fees) / qty

Probability framework (Brownian motion with drift):
  - X(t) = μt + σ×W(t) where μ is drift, σ is 4h stdev
  - Upper barrier u = log(TP / mark)  (LONG goal)
  - Lower barrier l = -log(SL / mark) actually use returns directly
  - For drift μ=0: P(hit u first) = |l| / (u + |l|)
  - For drift μ≠0: P(hit u first) = (1 - exp(-2μl/σ²)) / (exp(2μu/σ²) - exp(-2μl/σ²))
  - Expected hit time E[τ] = u × |l| / σ² (for μ=0)

Adjustments for crypto reality:
  - Fat tails (kurtosis): true probability of large moves > Gaussian. Multiply σ by 1.2-1.5
  - Mean reversion: drift toward EMA can favor counter-trend
  - Recent momentum: positive 24h drift suggests positive μ
  - Funding cost: deducts from net profit over time
"""
from dotenv import load_dotenv
load_dotenv()
import math
import argparse
from tradingagents.binance.client import spot_client


# Binance USDT-M futures taker fee (round trip)
TAKER_FEE = 0.0005  # 0.05% per side


def get_market_data(c, symbol, n_4h_candles=180):
    """Pull 4h klines + ticker + funding rate."""
    klines = c.futures_klines(symbol=symbol, interval="4h", limit=n_4h_candles)
    closes = [float(k[4]) for k in klines]
    returns_4h = [(closes[i] - closes[i-1]) / closes[i-1] for i in range(1, len(closes))]

    # Volatility: stdev of log returns (closer to GBM assumption)
    log_returns = [math.log(closes[i] / closes[i-1]) for i in range(1, len(closes))]
    mean_log_return = sum(log_returns) / len(log_returns)
    var = sum((r - mean_log_return)**2 for r in log_returns) / len(log_returns)
    sigma_4h = math.sqrt(var)

    # Recent drift from last 24h (6 candles)
    recent_returns = log_returns[-6:]
    drift_24h = sum(recent_returns) / len(recent_returns)  # avg per 4h

    # Funding
    prem = c.futures_mark_price(symbol=symbol)
    fr_8h = float(prem.get("lastFundingRate", 0))

    # Current price
    t = c.futures_symbol_ticker(symbol=symbol)
    mark = float(t["price"])

    return {
        "mark": mark,
        "sigma_4h_log": sigma_4h,
        "sigma_4h_pct": sigma_4h * 100,
        "mean_log_return_4h": mean_log_return,
        "drift_24h_log_per_4h": drift_24h,
        "fr_8h": fr_8h,
        "closes": closes,
        "log_returns": log_returns,
    }


def solve_tp_for_net(qty, entry, target_net_usd, open_fee, fee_rate=TAKER_FEE,
                      funding_per_cycle=0, n_cycles=1):
    """Given desired net profit, compute the required TP price."""
    # net = qty × (TP - entry) - open_fee - close_fee - funding × n
    # close_fee = qty × TP × fee_rate
    # Solve: qty × TP - qty × entry - open_fee - qty × TP × fee_rate - funding × n = target_net
    # qty × TP × (1 - fee_rate) = target_net + qty × entry + open_fee + funding × n
    numerator = target_net_usd + qty * entry + open_fee + funding_per_cycle * n_cycles
    tp = numerator / (qty * (1 - fee_rate))
    return tp


def hit_probability_brownian(mark, tp, sl, sigma_4h, drift_per_4h=0, fat_tail_mult=1.3):
    """Random walk probability of hitting TP before SL.
    Returns (P_tp_first, P_sl_first, expected_hit_time_4h)."""
    # Effective sigma with fat-tail adjustment
    sigma = sigma_4h * fat_tail_mult
    # In log space
    u = math.log(tp / mark)   # positive
    l = math.log(sl / mark)   # negative

    if drift_per_4h == 0:
        # Random walk no drift
        p_tp = -l / (u - l)
        p_sl = u / (u - l)
        # Expected time = u × |l| / σ²
        e_time_4h = u * abs(l) / (sigma**2) if sigma > 0 else 999
    else:
        # With drift
        m = drift_per_4h
        try:
            denom = math.exp(2 * m * u / sigma**2) - math.exp(-2 * m * (-l) / sigma**2)
            num = 1 - math.exp(-2 * m * (-l) / sigma**2)
            p_tp = num / denom if abs(denom) > 1e-10 else 0.5
            p_sl = 1 - p_tp
            # Approximate expected time
            e_time_4h = (u * abs(l) / (sigma**2 + m**2)) if (sigma**2 + m**2) > 0 else 999
        except OverflowError:
            p_tp = 0.5
            p_sl = 0.5
            e_time_4h = 999
    return p_tp, p_sl, e_time_4h


def historical_hit_freq(closes, mark, tp, sl, window_candles):
    """How often did historical price hit TP before SL in N candles?"""
    tp_pct = (tp - mark) / mark
    sl_pct = (sl - mark) / mark
    hits_tp = 0
    hits_sl = 0
    neither = 0
    for i in range(len(closes) - window_candles):
        start = closes[i]
        future = closes[i+1:i+window_candles+1]
        max_up = max((f - start) / start for f in future)
        max_down = min((f - start) / start for f in future)
        # Which barrier hit first chronologically
        hit_tp_time = None
        hit_sl_time = None
        for j, f in enumerate(future):
            ret = (f - start) / start
            if hit_tp_time is None and ret >= tp_pct:
                hit_tp_time = j
            if hit_sl_time is None and ret <= sl_pct:
                hit_sl_time = j
        if hit_tp_time is not None and (hit_sl_time is None or hit_tp_time < hit_sl_time):
            hits_tp += 1
        elif hit_sl_time is not None:
            hits_sl += 1
        else:
            neither += 1
    return hits_tp, hits_sl, neither, len(closes) - window_candles


def solve(symbol, qty, entry, current_sl, target_net_usd, time_horizon_hours=72,
          open_fee=None, confidence=0.65):
    """Full solver: find optimal TP given target net + time + confidence."""
    c = spot_client()
    data = get_market_data(c, symbol)
    mark = data["mark"]
    sigma_4h = data["sigma_4h_log"]
    drift_4h = data["drift_24h_log_per_4h"]
    fr_8h = data["fr_8h"]

    print(f"\n=== SOLVE TARGET {symbol} ===")
    print(f"Entry: ${entry}  Mark: ${mark}  Current SL: ${current_sl}  Qty: {qty}")
    print(f"Target NET profit: ${target_net_usd}  Time horizon: {time_horizon_hours}h  Confidence: {confidence*100:.0f}%")

    # Market characteristics
    print(f"\n--- Market data ---")
    print(f"  4h log-stdev: {sigma_4h*100:.2f}% (annualized ~{sigma_4h*math.sqrt(6*365)*100:.0f}%)")
    print(f"  Recent 24h drift per 4h: {drift_4h*100:+.3f}%")
    print(f"  Funding 8h: {fr_8h*100:+.4f}%")

    # Funding cost
    cycles = max(1, time_horizon_hours / 8)
    funding_per_cycle = qty * entry * abs(fr_8h)
    total_funding = funding_per_cycle * cycles
    direction_funding_cost = fr_8h * cycles  # LONG pays when positive

    # Estimate open fee if not provided
    if open_fee is None:
        open_fee = qty * entry * TAKER_FEE

    # Solve TP for target
    tp_needed = solve_tp_for_net(qty, entry, target_net_usd, open_fee,
                                   funding_per_cycle=funding_per_cycle, n_cycles=cycles)
    tp_pct = (tp_needed - mark) / mark * 100
    sl_pct = (current_sl - mark) / mark * 100

    print(f"\n--- Required TP for net ${target_net_usd} ---")
    print(f"  TP price: ${tp_needed:.4f}  ({tp_pct:+.2f}% from mark)")
    print(f"  SL: ${current_sl:.4f}  ({sl_pct:+.2f}% from mark)")
    print(f"  R:R = 1:{tp_pct/abs(sl_pct):.2f}")

    # Probability
    p_tp, p_sl, e_time = hit_probability_brownian(mark, tp_needed, current_sl,
                                                    sigma_4h, drift_4h)
    print(f"\n--- Probability (Brownian with drift) ---")
    print(f"  P(TP first): {p_tp*100:.1f}%")
    print(f"  P(SL first): {p_sl*100:.1f}%")
    print(f"  Expected hit time: {e_time:.1f} × 4h = {e_time*4:.0f}h = {e_time*4/24:.1f} days")

    # Historical check
    window = int(time_horizon_hours / 4)
    h_tp, h_sl, h_neither, h_total = historical_hit_freq(data["closes"], mark, tp_needed, current_sl, window)
    print(f"\n--- Historical {time_horizon_hours}h ({window} candles) ---")
    print(f"  TP first: {h_tp/h_total*100:.1f}% ({h_tp}/{h_total})")
    print(f"  SL first: {h_sl/h_total*100:.1f}% ({h_sl}/{h_total})")
    print(f"  Neither (still chopping): {h_neither/h_total*100:.1f}% ({h_neither}/{h_total})")

    # EV calc
    expected_loss_at_sl = qty * (current_sl - entry) - open_fee - qty * current_sl * TAKER_FEE - total_funding
    ev_random = p_tp * target_net_usd + p_sl * expected_loss_at_sl
    ev_historical = (h_tp/h_total) * target_net_usd + (h_sl/h_total) * expected_loss_at_sl + (h_neither/h_total) * 0
    print(f"\n--- Expected Value ---")
    print(f"  Profit at TP: +${target_net_usd:.2f}")
    print(f"  Loss at SL:   ${expected_loss_at_sl:.2f}")
    print(f"  EV (random walk): ${ev_random:+.2f}")
    print(f"  EV (historical):  ${ev_historical:+.2f}")

    # Recommendations
    print(f"\n--- VERDICT ---")
    if ev_historical > 0 and (h_tp/h_total) >= 0.30:
        print(f"  [OK] Setup profitable. EV positive, hit rate {h_tp/h_total*100:.0f}% >= 30%")
        print(f"  Recommended TP: ${tp_needed:.4f}")
    elif ev_historical > 0:
        print(f"  [MARGINAL] EV positive but hit rate low. May need patience.")
    else:
        print(f"  [SKIP] EV negative. Either: reduce target, increase qty, or skip.")
        # Suggest min target for positive EV
        # EV = p × X + (1-p) × loss = 0 -> X = -(1-p) × loss / p
        if p_tp > 0 and expected_loss_at_sl < 0:
            min_target_for_positive_ev = -p_sl * expected_loss_at_sl / p_tp
            print(f"  Minimum target for breakeven EV: ${min_target_for_positive_ev:.2f}")

    return {
        "tp_needed": tp_needed,
        "p_tp_brownian": p_tp,
        "p_tp_historical": h_tp / h_total if h_total > 0 else 0,
        "expected_hit_time_hours": e_time * 4,
        "ev_historical": ev_historical,
    }


if __name__ == "__main__":
    # Apply to current ASTER position
    solve(
        symbol="ASTERUSDT",
        qty=35,
        entry=0.6985,
        current_sl=0.68,
        target_net_usd=1.0,    # user's $1 challenge after fees
        time_horizon_hours=72,
    )
    print("\n" + "="*60)
    # Also test for $2 net target
    solve(
        symbol="ASTERUSDT",
        qty=35,
        entry=0.6985,
        current_sl=0.68,
        target_net_usd=2.0,
        time_horizon_hours=72,
    )
