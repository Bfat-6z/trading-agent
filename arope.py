"""AROPE — Analog Regime-Optimal Profit Estimator.

Novel framework:
  1. Extract 5-dim state vector per (coin, time): [vol_pct, RSI_1h, EMA_dist, funding, vol_ratio]
  2. For current state V_now, find K=20 nearest historical states by Euclidean distance
  3. From those analog periods, look at ACTUAL price movements 1h/4h/12h/24h forward
  4. Compute P(hit_target) + E[time_to_hit] from empirical distribution (not parametric)
  5. Cross-coin: rank by profit_potential / expected_time × P(hit)

Differences from standard approaches:
  - Random walk: assumes Gaussian increments, ignores regime
  - Historical raw: uses ALL past windows including non-analog regimes
  - GARCH/jump models: parametric, fits poorly on small-cap crypto
  - AROPE: non-parametric, conditional on regime, multi-asset comparable
"""
from dotenv import load_dotenv
load_dotenv()
import math
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from tradingagents.binance.client import spot_client


TAKER_FEE = 0.0005   # 0.05% per side


def compute_rsi(closes, period=14):
    """Standard 14-period RSI."""
    if len(closes) < period + 1:
        return 50
    gains = []; losses = []
    for i in range(1, len(closes)):
        diff = closes[i] - closes[i-1]
        if diff > 0: gains.append(diff); losses.append(0)
        else: gains.append(0); losses.append(-diff)
    avg_gain = sum(gains[-period:]) / period
    avg_loss = sum(losses[-period:]) / period
    if avg_loss == 0: return 100
    rs = avg_gain / avg_loss
    return 100 - 100 / (1 + rs)


def compute_ema(values, period):
    if len(values) < period: return values[-1]
    k = 2 / (period + 1)
    ema = sum(values[:period]) / period
    for v in values[period:]:
        ema = v * k + ema * (1 - k)
    return ema


def state_vector(klines_1h_window, current_price, vol_30d_avg_range, fr_8h):
    """Compute 5-dim state at end of klines_1h_window.
    All features normalized to roughly [0, 1] or [-1, 1]."""
    closes = [float(k[4]) for k in klines_1h_window]
    highs = [float(k[2]) for k in klines_1h_window]
    lows = [float(k[3]) for k in klines_1h_window]

    # 1. Volatility percentile (range last 24h / 30d avg range)
    if len(klines_1h_window) >= 24:
        h24 = max(highs[-24:])
        l24 = min(lows[-24:])
        rng_24h = (h24 - l24) / l24
        vol_pct = min(rng_24h / vol_30d_avg_range, 3.0) if vol_30d_avg_range > 0 else 1.0
    else:
        vol_pct = 1.0

    # 2. RSI 1h (last 14 closes)
    rsi = compute_rsi(closes[-15:]) / 100

    # 3. Price vs EMA50 (1h)
    ema50 = compute_ema(closes[-50:] if len(closes) >= 50 else closes, min(50, len(closes)-1))
    ema_dist = (current_price - ema50) / ema50 if ema50 > 0 else 0
    ema_dist_norm = max(-0.3, min(0.3, ema_dist))  # clip to [-30%, 30%]

    # 4. Funding (normalize to roughly [-1, 1])
    fr_norm = max(-1, min(1, fr_8h * 1000))  # 0.001 = +1.0

    # 5. Volume ratio (last 1h vol / 1h avg over window)
    vols = [float(k[7]) for k in klines_1h_window]
    avg_vol = sum(vols[-24:]) / 24 if len(vols) >= 24 else sum(vols) / len(vols)
    last_vol = vols[-1]
    vol_ratio = last_vol / avg_vol if avg_vol > 0 else 1.0
    vol_ratio_norm = min(vol_ratio / 3, 1.0)  # cap at 3x

    return [vol_pct, rsi, ema_dist_norm, fr_norm, vol_ratio_norm]


def euclidean_distance(v1, v2, weights=None):
    """Weighted Euclidean distance between two state vectors."""
    if weights is None:
        weights = [1.0] * len(v1)
    return math.sqrt(sum(w * (a - b)**2 for a, b, w in zip(v1, v2, weights)))


def forecast_coin(symbol, target_net_usd, qty_dollars_notional=20,
                    horizons_hours=[4, 8, 12, 24, 48], k=20):
    """Run AROPE on one coin. Return forecast dict.
    qty_dollars_notional: how much capital position size in dollars."""
    c = spot_client()

    # Pull 30 days of 1h klines
    klines = c.futures_klines(symbol=symbol, interval="1h", limit=720)
    if len(klines) < 100:
        return {"symbol": symbol, "error": f"insufficient data ({len(klines)} candles)"}

    closes = [float(k[4]) for k in klines]
    current_price = closes[-1]

    # 30d average daily range for vol normalization
    daily_ranges = []
    for i in range(24, len(klines), 24):
        window = klines[i-24:i]
        highs = [float(k[2]) for k in window]
        lows = [float(k[3]) for k in window]
        if highs and lows:
            daily_ranges.append((max(highs) - min(lows)) / min(lows))
    avg_daily_range = sum(daily_ranges) / len(daily_ranges) if daily_ranges else 0.05

    # Current funding rate
    try:
        prem = c.futures_mark_price(symbol=symbol)
        fr_8h = float(prem.get("lastFundingRate", 0))
    except Exception:
        fr_8h = 0

    # Current state
    v_now = state_vector(klines[-50:], current_price, avg_daily_range, fr_8h)

    # Build historical states + corresponding future returns
    # For each historical hour t (with at least 50 prior candles), compute state at t
    # Then look at returns t+h for h in horizons
    historical_states = []
    max_horizon = max(horizons_hours)
    for i in range(50, len(klines) - max_horizon):
        window = klines[i-50:i+1]
        price_at_t = float(klines[i][4])
        # Use historical funding (we don't have hist funding so use current as proxy)
        v_t = state_vector(window, price_at_t, avg_daily_range, fr_8h)
        # Future max/min returns for each horizon
        future_data = {}
        for h in horizons_hours:
            future_window = klines[i+1:i+h+1]
            future_highs = [float(k[2]) for k in future_window]
            future_lows = [float(k[3]) for k in future_window]
            if future_highs and future_lows:
                max_up = max(f / price_at_t - 1 for f in future_highs)
                max_down = min(f / price_at_t - 1 for f in future_lows)
                # Time to max up within window
                time_to_max_up = None
                for j, f in enumerate(future_highs):
                    if f / price_at_t - 1 >= max_up - 1e-6:
                        time_to_max_up = j + 1
                        break
                future_data[h] = {"max_up": max_up, "max_down": max_down,
                                  "t_to_max_up": time_to_max_up or h}
        historical_states.append((v_t, future_data, i))

    if len(historical_states) < k:
        return {"symbol": symbol, "error": f"too few historical states ({len(historical_states)})"}

    # Find K nearest
    distances = [(euclidean_distance(v_now, v_t), idx, fd) for idx, (v_t, fd, _) in enumerate(historical_states)]
    distances.sort(key=lambda x: x[0])
    analogs = distances[:k]

    # Compute hit probability for target net at each horizon
    # Required gross move: (target_net + 2 × fees) / notional
    fees_total = qty_dollars_notional * TAKER_FEE * 2
    required_move = (target_net_usd + fees_total) / qty_dollars_notional

    forecasts = {}
    for h in horizons_hours:
        hit_count = 0
        hit_times = []
        actual_max_ups = []
        for _, idx, fd in analogs:
            if h not in fd: continue
            max_up = fd[h]["max_up"]
            actual_max_ups.append(max_up)
            if max_up >= required_move:
                hit_count += 1
                hit_times.append(fd[h]["t_to_max_up"])
        if not actual_max_ups: continue
        p_hit = hit_count / len(actual_max_ups)
        e_time = sum(hit_times) / len(hit_times) if hit_times else h
        p25 = sorted(actual_max_ups)[len(actual_max_ups) // 4]
        p75 = sorted(actual_max_ups)[3 * len(actual_max_ups) // 4]
        forecasts[h] = {
            "p_hit": p_hit,
            "expected_hit_time_h": e_time if hit_times else None,
            "max_up_p25": p25,
            "max_up_p75": p75,
            "required_move_pct": required_move * 100,
        }

    return {
        "symbol": symbol,
        "current_price": current_price,
        "state_vector": v_now,
        "avg_daily_range_pct": avg_daily_range * 100,
        "funding_8h_pct": fr_8h * 100,
        "required_move_for_target_pct": required_move * 100,
        "forecasts": forecasts,
        "n_analogs": len(analogs),
        "n_historical": len(historical_states),
    }


def rank_coins(symbols, target_net_usd, qty_notional=20, time_horizon_h=24):
    """Run AROPE on multiple coins, rank by expected profit per hour."""
    print(f"\n=== AROPE Ranking — target net ${target_net_usd}, horizon {time_horizon_h}h ===\n")
    results = []

    def worker(sym):
        try:
            return forecast_coin(sym, target_net_usd, qty_notional,
                                   horizons_hours=[time_horizon_h])
        except Exception as e:
            return {"symbol": sym, "error": f"{type(e).__name__}: {str(e)[:60]}"}

    # Parallel for speed
    with ThreadPoolExecutor(max_workers=5) as ex:
        futs = {ex.submit(worker, s): s for s in symbols}
        for f in as_completed(futs):
            r = f.result(timeout=60)
            results.append(r)

    # Rank by p_hit / expected_time
    ranked = []
    for r in results:
        if "error" in r:
            continue
        fc = r["forecasts"].get(time_horizon_h)
        if not fc: continue
        p_hit = fc["p_hit"]
        e_time = fc["expected_hit_time_h"] or time_horizon_h
        required_move = fc["required_move_pct"]
        # Score: expected dollars per hour
        # If hit: +target / e_time hours
        # If not hit: 0 from this trade
        ev_per_hour = (p_hit * target_net_usd) / e_time if e_time > 0 else 0
        ranked.append({
            **r,
            "p_hit": p_hit,
            "e_time_h": e_time,
            "required_move_pct": required_move,
            "ev_per_hour": ev_per_hour,
        })

    ranked.sort(key=lambda x: x["ev_per_hour"], reverse=True)
    print(f"{'Symbol':14s} {'P(hit)':>7s} {'E[time]':>8s} {'Req':>7s} {'$/hr':>7s} {'ADR':>6s}")
    print("-" * 70)
    for r in ranked[:15]:
        print(f"  {r['symbol']:14s} {r['p_hit']*100:>5.0f}%  {r['e_time_h']:>5.1f}h  "
              f"{r['required_move_pct']:>5.1f}% {r['ev_per_hour']:>6.3f}  {r['avg_daily_range_pct']:>5.1f}%")

    return ranked


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "scan":
        # Scan top movers + rank
        c = spot_client()
        tickers = c.futures_ticker()
        EXCL = {'USDC','FDUSD','TUSD','BUSD','DAI','USDP','EUR','XAU','XAG','NVDA','TSLA','AAPL','MSFT','GOOGL','META','AMZN','INTC','AMD','SOXL','MSTR','COIN','HOOD','RIOT'}
        candidates = []
        for t in tickers:
            sym = t.get('symbol','')
            if not sym.endswith('USDT') or sym[:-4] in EXCL: continue
            try:
                vol = float(t.get('quoteVolume',0))/1e6
                if vol < 150: continue
                candidates.append(sym)
            except: continue
        candidates = candidates[:25]  # cap to avoid TV/API rate limit
        print(f"Scanning {len(candidates)} candidates with vol > $150M...")
        rank_coins(candidates, target_net_usd=2.0, qty_notional=20, time_horizon_h=24)
    else:
        # Single coin demo
        r = forecast_coin("ASTERUSDT", 1.0, 20, [4, 12, 24, 48])
        print(json.dumps(r, indent=2, default=str))
