"""AROPE v2 — Analog Regime-Optimal Profit Estimator (with microstructure).

Improvements over v1:
  1. 8-dim state vector includes microstructure (OI change, buy/sell pressure, session)
  2. Uses HISTORICAL funding rate (not just current)
  3. Buy/sell pressure from kline taker volume (not just count)
  4. Session-aware (US session more volatile)
  5. Feature weighting for k-NN balance

State vector V_t = [
    vol_pct_24h,        # local volatility
    RSI_1h,             # momentum
    EMA_dist_1h,        # trend extension
    funding_rate,       # positioning
    volume_ratio,       # interest level
    OI_24h_change,      # NEW: are positions opening or covering?
    buy_pressure_1h,    # NEW: order flow direction
    in_us_session       # NEW: time-of-day context
]

For LONG: find analogs, check P(price >= target × upmove within horizon)
For SHORT: same but check P(price <= -target × downmove)
"""
from dotenv import load_dotenv
load_dotenv()
import math
import json
import time as time_module
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from tradingagents.binance.client import spot_client


TAKER_FEE = 0.0005


def compute_rsi(closes, period=14):
    if len(closes) < period + 1: return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    ag = sum(gains[-period:]) / period
    al = sum(losses[-period:]) / period
    if al == 0: return 100
    return 100 - 100 / (1 + ag/al)


def compute_ema(values, period):
    if len(values) < period: return values[-1] if values else 0
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def state_vector_v2(klines_1h_window, oi_at_t, oi_at_t_minus_24, funding_at_t, vol_30d_avg_range,
                     kline_timestamp_ms):
    """8-dim state at end of klines window. ts_ms is end timestamp."""
    if len(klines_1h_window) < 25:
        return None
    closes = [float(k[4]) for k in klines_1h_window]
    highs = [float(k[2]) for k in klines_1h_window]
    lows = [float(k[3]) for k in klines_1h_window]
    quote_vols = [float(k[7]) for k in klines_1h_window]
    taker_buy_vols = [float(k[10]) for k in klines_1h_window]  # taker buy quote vol

    current_price = closes[-1]

    # 1. Vol percentile
    h24 = max(highs[-24:]); l24 = min(lows[-24:])
    rng = (h24 - l24) / l24 if l24 > 0 else 0
    f1 = min(rng / vol_30d_avg_range, 3.0) if vol_30d_avg_range > 0 else 1.0

    # 2. RSI 1h (-50 + 50 normalized to [-1, 1])
    f2 = (compute_rsi(closes[-15:]) - 50) / 50

    # 3. EMA dist 1h
    ema50 = compute_ema(closes, min(50, len(closes)-1))
    f3 = max(-0.3, min(0.3, (current_price - ema50) / ema50 if ema50 > 0 else 0))

    # 4. Funding (clamped to [-1, 1])
    f4 = max(-1, min(1, funding_at_t * 1000))

    # 5. Volume ratio (recent vs window avg)
    avg_vol = sum(quote_vols[-24:]) / 24
    f5 = min((quote_vols[-1] / avg_vol if avg_vol > 0 else 1) / 3, 1.0)

    # 6. OI change 24h (clamped to [-0.5, 0.5])
    if oi_at_t_minus_24 and oi_at_t_minus_24 > 0:
        oi_chg = (oi_at_t - oi_at_t_minus_24) / oi_at_t_minus_24
        f6 = max(-0.5, min(0.5, oi_chg))
    else:
        f6 = 0

    # 7. Buy pressure (last 1h taker buy / total)
    last_buy = taker_buy_vols[-1]
    last_total = quote_vols[-1]
    f7 = (last_buy / last_total) if last_total > 0 else 0.5
    f7 = (f7 - 0.5) * 2  # normalize to [-1, 1]

    # 8. US session (UTC 16-24)
    hour_utc = datetime.utcfromtimestamp(kline_timestamp_ms / 1000).hour
    f8 = 1.0 if 16 <= hour_utc < 24 else 0.0

    return [f1, f2, f3, f4, f5, f6, f7, f8]


# Feature importance weights for distance calculation
FEATURE_WEIGHTS = [1.0, 1.5, 1.5, 1.0, 1.0, 1.5, 2.0, 0.5]
# RSI, EMA_dist, OI_change, buy_pressure get higher weight
# US session gets lower (less discriminating)


def euclidean_weighted(v1, v2):
    return math.sqrt(sum(w*(a-b)**2 for a, b, w in zip(v1, v2, FEATURE_WEIGHTS)))


def fetch_oi_hist(c, symbol):
    """Fetch 30 days of hourly OI. Returns dict {timestamp_ms: oi}."""
    try:
        oi_hist = c.futures_open_interest_hist(symbol=symbol, period="1h", limit=500)
        return {int(o["timestamp"]): float(o["sumOpenInterest"]) for o in oi_hist}
    except Exception:
        return {}


def fetch_funding_hist(c, symbol):
    """Fetch funding rate history. Returns list of (ts, rate) sorted by ts."""
    try:
        fr_hist = c.futures_funding_rate(symbol=symbol, limit=200)
        return sorted([(int(f["fundingTime"]), float(f["fundingRate"])) for f in fr_hist])
    except Exception:
        return []


def funding_at_time(funding_hist, ts_ms):
    """Find funding rate closest to (but before) ts_ms. Returns rate or 0."""
    if not funding_hist: return 0
    # Binary search
    lo, hi = 0, len(funding_hist) - 1
    if ts_ms <= funding_hist[0][0]: return funding_hist[0][1]
    if ts_ms >= funding_hist[-1][0]: return funding_hist[-1][1]
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if funding_hist[mid][0] <= ts_ms: lo = mid
        else: hi = mid - 1
    return funding_hist[lo][1]


def oi_at_time(oi_hist, ts_ms, tolerance_ms=3600 * 1000):
    """Find OI value at or close to ts_ms. Returns None if no nearby."""
    if not oi_hist: return None
    # exact match
    if ts_ms in oi_hist: return oi_hist[ts_ms]
    # nearest within tolerance
    candidates = [t for t in oi_hist.keys() if abs(t - ts_ms) <= tolerance_ms]
    if not candidates: return None
    nearest = min(candidates, key=lambda t: abs(t - ts_ms))
    return oi_hist[nearest]


def forecast_v2(symbol, target_net_usd, qty_notional, direction="LONG",
                 horizons=[4, 8, 12, 24, 48], k=15):
    """AROPE v2 forecast with full microstructure features."""
    c = spot_client()
    klines = c.futures_klines(symbol=symbol, interval="1h", limit=720)
    if len(klines) < 100:
        return {"symbol": symbol, "error": f"insufficient klines ({len(klines)})"}

    closes = [float(k[4]) for k in klines]
    timestamps = [int(k[0]) for k in klines]
    current_price = closes[-1]

    # Pre-fetch microstructure history
    oi_hist = fetch_oi_hist(c, symbol)
    funding_hist = fetch_funding_hist(c, symbol)

    # 30d avg daily range
    daily_ranges = []
    for i in range(24, len(klines), 24):
        w_highs = [float(k[2]) for k in klines[i-24:i]]
        w_lows = [float(k[3]) for k in klines[i-24:i]]
        if w_highs and w_lows:
            daily_ranges.append((max(w_highs) - min(w_lows)) / min(w_lows))
    avg_dr = sum(daily_ranges) / len(daily_ranges) if daily_ranges else 0.05

    # Current state
    current_funding = funding_hist[-1][1] if funding_hist else 0
    current_oi = oi_at_time(oi_hist, timestamps[-1])
    current_oi_24h_ago = oi_at_time(oi_hist, timestamps[-1] - 86400000)

    v_now = state_vector_v2(
        klines[-50:], current_oi, current_oi_24h_ago,
        current_funding, avg_dr, timestamps[-1]
    )
    if v_now is None:
        return {"symbol": symbol, "error": "couldn't compute current state"}

    # Build historical state list
    historical = []
    max_h = max(horizons)
    for i in range(50, len(klines) - max_h):
        ts = timestamps[i]
        oi_t = oi_at_time(oi_hist, ts)
        oi_t_24 = oi_at_time(oi_hist, ts - 86400000)
        if oi_t is None or oi_t_24 is None: continue
        funding_t = funding_at_time(funding_hist, ts)
        v_t = state_vector_v2(
            klines[i-50:i+1], oi_t, oi_t_24, funding_t, avg_dr, ts
        )
        if v_t is None: continue

        price_t = closes[i]
        future_data = {}
        for h in horizons:
            f_window = klines[i+1:i+h+1]
            f_highs = [float(k[2]) for k in f_window]
            f_lows = [float(k[3]) for k in f_window]
            if not f_highs: continue
            max_up = max(f / price_t - 1 for f in f_highs)
            max_dn = min(f / price_t - 1 for f in f_lows)
            # Time to max
            t_to_up = next((j+1 for j, f in enumerate(f_highs) if f/price_t-1 >= max_up - 1e-6), h)
            t_to_dn = next((j+1 for j, f in enumerate(f_lows) if f/price_t-1 <= max_dn + 1e-6), h)
            future_data[h] = {"max_up": max_up, "max_down": max_dn,
                                "t_to_max_up": t_to_up, "t_to_max_down": t_to_dn}
        historical.append((v_t, future_data, i))

    if len(historical) < k:
        return {"symbol": symbol, "error": f"only {len(historical)} valid historical states"}

    # K-NN
    distances = [(euclidean_weighted(v_now, v_t), fd) for v_t, fd, _ in historical]
    distances.sort(key=lambda x: x[0])
    analogs = distances[:k]

    fees_total = qty_notional * TAKER_FEE * 2
    required_move = (target_net_usd + fees_total) / qty_notional

    forecasts = {}
    for h in horizons:
        # For LONG: check max_up
        # For SHORT: check max_down (negative threshold)
        hits = 0
        hit_times = []
        moves = []
        for _, fd in analogs:
            if h not in fd: continue
            if direction == "LONG":
                key = "max_up"; t_key = "t_to_max_up"
                hit = fd[h][key] >= required_move
            else:
                key = "max_down"; t_key = "t_to_max_down"
                hit = fd[h][key] <= -required_move
            moves.append(fd[h][key])
            if hit:
                hits += 1
                hit_times.append(fd[h][t_key])
        if not moves: continue
        p_hit = hits / len(moves)
        e_time = sum(hit_times)/len(hit_times) if hit_times else None
        forecasts[h] = {
            "p_hit": p_hit,
            "expected_hit_time_h": e_time,
            "median_move": sorted(moves)[len(moves)//2],
            "best_quartile_move": sorted(moves)[3*len(moves)//4] if direction=="LONG" else sorted(moves)[len(moves)//4],
            "required_move_pct": required_move * 100,
        }

    return {
        "symbol": symbol,
        "direction": direction,
        "current_price": current_price,
        "current_state": v_now,
        "avg_daily_range_pct": avg_dr * 100,
        "current_funding_pct": current_funding * 100,
        "required_move_pct": required_move * 100,
        "forecasts": forecasts,
        "k_used": k,
    }


def rank_v2(symbols, target_net_usd, qty_notional, direction="LONG",
             horizon=24, verbose=True):
    """Rank multiple coins by EV per hour."""
    if verbose: print(f"\n=== AROPE v2 ranking — target ${target_net_usd} {direction} {horizon}h ===\n")
    results = []
    def worker(s):
        try:
            return forecast_v2(s, target_net_usd, qty_notional, direction, [horizon])
        except Exception as e:
            return {"symbol": s, "error": f"{type(e).__name__}: {str(e)[:60]}"}
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(worker, s): s for s in symbols}
        for f in as_completed(futs):
            try:
                results.append(f.result(timeout=120))
            except Exception: pass

    ranked = []
    for r in results:
        if "error" in r:
            if verbose: print(f"  {r['symbol']:14s} ERR: {r['error'][:50]}")
            continue
        fc = r["forecasts"].get(horizon)
        if not fc: continue
        p = fc["p_hit"]
        et = fc["expected_hit_time_h"] or horizon
        ev_per_hour = (p * target_net_usd) / et if et > 0 else 0
        ranked.append({**r, "p_hit": p, "e_time": et, "ev_per_hour": ev_per_hour,
                        "required_pct": fc["required_move_pct"]})
    ranked.sort(key=lambda x: x["ev_per_hour"], reverse=True)

    if verbose:
        print(f"\n{'Symbol':14s} {'Dir':>5s} {'P(hit)':>7s} {'E[t]':>6s} {'Req':>6s} {'$/hr':>7s} {'ADR':>6s} {'OI':>7s}")
        print("-" * 75)
        for r in ranked[:15]:
            oi_chg = r["current_state"][5] * 100  # f6 (clipped)
            print(f"  {r['symbol']:14s} {r['direction']:>5s} {r['p_hit']*100:>5.0f}%  "
                    f"{r['e_time']:>4.1f}h {r['required_pct']:>5.1f}% {r['ev_per_hour']:>6.3f}  "
                    f"{r['avg_daily_range_pct']:>5.1f}%  {oi_chg:>+5.0f}%")
    return ranked


if __name__ == "__main__":
    import sys
    if "scan" in sys.argv:
        c = spot_client()
        tickers = c.futures_ticker()
        EXCL = {'USDC','FDUSD','TUSD','BUSD','DAI','USDP','EUR','XAU','XAG','NVDA','TSLA','AAPL','MSFT','GOOGL','META','AMZN','INTC','AMD','SOXL','MSTR','COIN','HOOD','RIOT'}
        cands = []
        for t in tickers:
            sym = t.get('symbol','')
            if not sym.endswith('USDT') or sym[:-4] in EXCL: continue
            try:
                vol = float(t.get('quoteVolume',0))/1e6
                if vol < 100: continue
                cands.append(sym)
            except: continue
        cands = cands[:20]
        print(f"Scanning {len(cands)} candidates LONG direction...")
        long_ranked = rank_v2(cands, 2.0, 20, "LONG", 24)
        print(f"\nScanning {len(cands)} candidates SHORT direction...")
        short_ranked = rank_v2(cands, 2.0, 20, "SHORT", 24)
        print("\n=== BEST OF BOTH DIRECTIONS ===")
        all_ranked = long_ranked + short_ranked
        all_ranked.sort(key=lambda x: x["ev_per_hour"], reverse=True)
        for r in all_ranked[:5]:
            print(f"  {r['symbol']:14s} {r['direction']:5s} P={r['p_hit']*100:.0f}% E_t={r['e_time']:.1f}h ${r['ev_per_hour']:.3f}/hr ADR={r['avg_daily_range_pct']:.1f}%")
    else:
        print(json.dumps(forecast_v2("ASTERUSDT", 1.0, 20, "LONG", [4,12,24,48]), indent=2, default=str))
