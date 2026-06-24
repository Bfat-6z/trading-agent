"""Chart-focused scan: pull TV multi-TF for top movers, score for cleanest setup.
Filters that go beyond just RSI/MACD — look for proper chart structure."""
from dotenv import load_dotenv
load_dotenv()
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from tradingagents.binance.client import spot_client
from tradingagents.crypto.tv_data import fetch_tv_multi_tf

EXCLUDE = {
    "USDC","FDUSD","TUSD","BUSD","DAI","USDP","EUR",
    "XAU","XAG","NVDA","TSLA","AAPL","MSFT","GOOGL","GOOG","META","AMZN",
    "NFLX","INTC","AMD","INTU","CRM","ORCL","DIS","JPM","BAC","V","MA","KO",
    "PEP","WMT","MCD","HD","NKE","BA","GE","F","GM",
    "SOXL","SOXX","QQQ","SPY","IWM","INX","TQQQ","SQQQ","UVXY",
    "GLD","SLV","USO","TLT","SNDK","VVV","MSTR","COIN","HOOD","RIOT","MARA","SQ",
}


def fetch_universe():
    c = spot_client()
    tickers = c.futures_ticker()
    candidates = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"): continue
        base = sym[:-4]
        if base in EXCLUDE: continue
        try:
            vol_m = float(t.get("quoteVolume", 0)) / 1e6
            ch = float(t.get("priceChangePercent", 0))
            cnt = int(t.get("count", 0))
            if vol_m < 10 or cnt < 5000: continue   # tighter vol filter
            if abs(ch) > 30: continue   # skip parabolics both directions
            high = float(t["highPrice"]); low = float(t["lowPrice"]); price = float(t["lastPrice"])
            rng_pos = (price - low) / (high - low) if high > low else 0.5
            candidates.append({"symbol": sym, "ch24": ch, "vol_m": vol_m, "rng_pos": rng_pos, "price": price})
        except Exception: continue
    # Top by composite: balanced range + decent volume
    candidates.sort(key=lambda x: x["vol_m"], reverse=True)
    return candidates[:50]


def score_chart_setup(tv_data, ch24, rng_pos):
    """Score chart quality 0-10. Higher = cleaner setup.
    Considers: TF alignment, trend health, NOT extended, breakout/reversal patterns."""
    if not tv_data: return 0, "no_tv", None
    tfs = tv_data["timeframes"]
    h15 = tfs.get("15m", {}); h1 = tfs.get("1h", {}); h4 = tfs.get("4h", {}); d1 = tfs.get("1D", {})
    if not all(tf.get("available") for tf in [h1, h4, d1]):
        return 0, "incomplete_tv", None

    score_long = 0; score_short = 0; flags = []

    # === TREND ALIGNMENT ===
    # All TFs trending up = bull bias
    if h1["ema_trend_short"]=="up" and h4["ema_trend_short"]=="up" and d1["ema_trend_short"]=="up":
        score_long += 2; flags.append("trends_aligned_up")
    if h1["ema_trend_short"]=="down" and h4["ema_trend_short"]=="down" and d1["ema_trend_short"]=="down":
        score_short += 2; flags.append("trends_aligned_down")

    # === NOT EXTENDED ===
    # 4h price < 8% above EMA20 = room to go up
    if -1 < h4["price_vs_ema20_pct"] < 8:
        score_long += 1.5
    if -8 < h4["price_vs_ema20_pct"] < 1:
        score_short += 1.5

    # === RSI Sweet spot ===
    # LONG: 1h RSI 45-65 (uptrend, not OB), 4h RSI < 75
    if 45 <= h1["rsi"] <= 65 and h4["rsi"] < 75:
        score_long += 1.5
    # SHORT: 1h RSI 35-55 (downtrend, not OS), 4h RSI > 25
    if 35 <= h1["rsi"] <= 55 and h4["rsi"] > 25:
        score_short += 1.5

    # === MACD ALIGNMENT ===
    # Both 1h + 4h MACD positive = bull momentum
    if h1["macd_hist"] > 0 and h4["macd_hist"] > 0:
        score_long += 1
    if h1["macd_hist"] < 0 and h4["macd_hist"] < 0:
        score_short += 1

    # MACD bullish cross 15m = early reversal
    if h15.get("available") and h15["macd_cross"] == "bullish_cross":
        score_long += 1; flags.append("15m_macd_bullcross")
    if h15.get("available") and h15["macd_cross"] == "bearish_cross":
        score_short += 1; flags.append("15m_macd_bearcross")

    # === ADX = trend strength ===
    # ADX 20-40 = healthy trend (not yet exhausted)
    h1_adx = h1["adx"]
    if 20 <= h1_adx <= 40:
        if h1["di_plus"] > h1["di_minus"]:
            score_long += 1
        else:
            score_short += 1
    elif h1_adx > 50:
        # Very strong trend — might be late, both sides penalized
        flags.append(f"adx_extreme_{h1_adx:.0f}")
        score_long -= 1; score_short -= 1

    # === BB POSITION ===
    # LONG: 4h BB 25-65% = healthy uptrend not overstretched
    if 25 <= h4["bb_position_pct"] <= 65:
        score_long += 1
    if h4["bb_position_pct"] > 90:
        # Overbought, NO LONG
        score_long -= 2; flags.append("4h_BB_overbought")
        score_short += 0.5
    if h4["bb_position_pct"] < 10:
        score_short -= 2; flags.append("4h_BB_oversold")
        score_long += 0.5

    # === VOLATILITY SANITY ===
    # 1D ATR 3-12% = normal, >18% = noise nightmare
    atr_1d = d1["atr_pct"]
    if atr_1d > 15:
        flags.append(f"high_atr_1d_{atr_1d:.0f}%")
        score_long -= 0.5; score_short -= 0.5

    # === ch24 vs ATR normalized momentum ===
    if atr_1d > 0:
        mult = ch24 / atr_1d
        # LONG: ch24 / ATR between -0.5 and +1.5 = healthy momentum, not extended
        if -0.5 <= mult <= 1.5:
            score_long += 1
        # SHORT: ch24 / ATR between -1.5 and +0.5
        if -1.5 <= mult <= 0.5:
            score_short += 1
        # Block: too extended either way
        if mult > 2.5:
            score_long -= 2; flags.append(f"momentum_{mult:.1f}x_atr_extended_up")
        if mult < -2.5:
            score_short -= 2; flags.append(f"momentum_{mult:.1f}x_atr_extended_down")

    side = "LONG" if score_long > score_short else "SHORT" if score_short > score_long else "NONE"
    score = max(score_long, score_short)
    return score, side, {
        "long": score_long, "short": score_short, "flags": flags,
        "h1_rsi": h1["rsi"], "h4_rsi": h4["rsi"], "d1_rsi": d1["rsi"],
        "h4_bb": h4["bb_position_pct"], "h4_ema": h4["price_vs_ema20_pct"],
        "atr_1d": atr_1d,
    }


def main():
    print("\n=== Tier 1: Top 50 by volume ===\n")
    t0 = time.time()
    universe = fetch_universe()
    print(f"  Fetched {len(universe)} candidates in {time.time()-t0:.1f}s")

    print("\n=== Tier 2: TV indicator pull + chart score ===\n")
    t1 = time.time()
    results = []
    def worker(c):
        try:
            tv = fetch_tv_multi_tf(c["symbol"])
            score, side, detail = score_chart_setup(tv, c["ch24"], c["rng_pos"])
            return {**c, "score": score, "side": side, "detail": detail}
        except Exception as e:
            return {**c, "score": 0, "side": "ERR", "detail": str(e)[:60]}

    with ThreadPoolExecutor(max_workers=10) as ex:
        futs = {ex.submit(worker, c): c for c in universe}
        for fut in as_completed(futs):
            try:
                results.append(fut.result(timeout=30))
            except Exception: continue
    print(f"  Scored {len(results)} in {time.time()-t1:.1f}s")

    # Sort by score
    results.sort(key=lambda x: x["score"], reverse=True)
    print("\n=== TOP 15 by chart quality ===\n")
    for i, r in enumerate(results[:15], 1):
        d = r.get("detail")
        if d is None or isinstance(d, str):
            note = d if isinstance(d, str) else "no_tv_data"
            print(f"  {i:2}. {r['symbol']:14s} ch24={r['ch24']:+6.2f}% vol={r['vol_m']:5.0f}M  side={r['side']:5s} score={r['score']:.1f}  ({note})")
            continue
        flags = ', '.join(d.get("flags", []))[:60]
        print(f"  {i:2}. {r['symbol']:14s} ch24={r['ch24']:+6.2f}% vol={r['vol_m']:5.0f}M  "
              f"side={r['side']:5s} score={r['score']:.1f}  "
              f"L={d['long']:.1f}/S={d['short']:.1f}")
        print(f"      1h_RSI={d['h1_rsi']:.0f} 4h_RSI={d['h4_rsi']:.0f} 1D_RSI={d['d1_rsi']:.0f}  "
              f"4h_BB={d['h4_bb']:.0f}% 4h_EMA={d['h4_ema']:+.1f}%  "
              f"ATR_1d={d['atr_1d']:.1f}%  flags=[{flags}]")

    with open("state/chart_scan_top10.json", "w") as f:
        json.dump([{**r, "detail": r["detail"] if isinstance(r["detail"], dict) else None}
                    for r in results[:10]], f, indent=2)
    print("\nTop 10 saved to state/chart_scan_top10.json")


if __name__ == "__main__":
    main()
