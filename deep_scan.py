"""Deep scan: Binance + TV multi-tier filter to find best trade setup.

Tier 1: All USDT-M perps -> Binance ticker filter (no LLM, no TV)
Tier 2: TV indicators for top 25 -> mechanical confluence score
Tier 3 (separate): pipeline.run on top 5
"""
from dotenv import load_dotenv
load_dotenv()
import json
import math
import time
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from tradingagents.binance.client import spot_client
from tradingagents.crypto.tv_data import fetch_tv_multi_tf, tv_summary_text

# Mirror EXCLUDE list from futures_watch.py
EXCLUDE_BASES = {
    "USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "EUR",
    "XAU", "XAG",
    "NVDA", "TSLA", "AAPL", "MSFT", "GOOGL", "GOOG", "META", "AMZN",
    "NFLX", "INTC", "AMD", "INTU", "CRM", "ORCL", "DIS",
    "JPM", "BAC", "V", "MA", "KO", "PEP", "WMT", "MCD", "HD", "NKE",
    "BA", "GE", "F", "GM",
    "SOXL", "SOXX", "QQQ", "SPY", "IWM", "INX", "TQQQ", "SQQQ", "UVXY",
    "GLD", "SLV", "USO", "TLT",
    "SNDK", "VVV", "MSTR", "COIN", "HOOD", "RIOT", "MARA", "SQ",
}
try:
    with open("state/tradfi_blacklist.json") as f:
        EXCLUDE_BASES |= set(json.load(f))
except Exception:
    pass

MIN_VOL_M = 3.0          # min $3M daily volume
MAX_GAIN = 35            # skip if >+35% (way too overheated for either dir reliably)
MAX_LOSS = -25           # skip if <-25%


def tier1_scan(top_n=25):
    """Pure Binance ticker filter. Output: ranked list of dicts."""
    c = spot_client()
    tickers = c.futures_ticker()
    candidates = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"):
            continue
        base = sym[:-4]
        if base in EXCLUDE_BASES:
            continue
        try:
            vol_m = float(t.get("quoteVolume", 0)) / 1e6
            ch = float(t.get("priceChangePercent", 0))
            cnt = int(t.get("count", 0))
            if vol_m < MIN_VOL_M or cnt < 3000:
                continue
            if ch > MAX_GAIN or ch < MAX_LOSS:
                continue
            high = float(t["highPrice"])
            low = float(t["lowPrice"])
            price = float(t["lastPrice"])
            rng_pos = (price - low) / (high - low) if high > low else 0.5

            # Setup classification (similar to futures_watch but more setups)
            vol_for_score = min(vol_m, 30)
            log_v = math.log10(max(vol_for_score * 1e6, 1)) / 7.5

            setup = None
            regime = 0.4
            if -15 <= ch <= -3 and rng_pos > 0.5:
                setup = "oversold_bounce_LONG"; regime = 1.7
            elif 3 <= ch <= 8 and 0.3 < rng_pos < 0.7:
                setup = "healthy_momentum_LONG"; regime = 1.5
            elif 8 < ch <= 14 and rng_pos > 0.6:
                setup = "momentum_continuation_LONG"; regime = 1.4
            elif 14 < ch <= 22 and rng_pos > 0.8:
                setup = "exhaustion_SHORT"; regime = 1.5
            elif 22 < ch <= 35 and rng_pos > 0.85:
                setup = "blow_off_SHORT"; regime = 1.6
            elif -3 < ch < 3 and 0.4 < rng_pos < 0.6:
                setup = "consolidation"; regime = 0.8
            else:
                setup = "other"; regime = 0.5

            candidates.append({
                "symbol": sym, "base": base,
                "price": price, "ch24": ch, "vol_m": vol_m, "rng_pos": rng_pos,
                "setup": setup, "score": log_v * regime,
            })
        except Exception:
            continue

    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates[:top_n]


def tier2_score_tv(symbol):
    """Pull TV multi-TF, score mechanically."""
    tv = fetch_tv_multi_tf(symbol)
    if not tv:
        return None, 0, 0, "(TV unavailable)"
    tfs = tv["timeframes"]
    long_score = 0.0
    short_score = 0.0
    notes = []
    weights = {"15m": 0.6, "1h": 1.0, "4h": 1.5, "1D": 1.2}
    for tf_name, w in weights.items():
        tf = tfs.get(tf_name)
        if not tf or not tf.get("available"):
            continue
        rsi = tf["rsi"]
        bb = tf["bb_position_pct"]
        ema_dist = tf["price_vs_ema20_pct"]
        adx = tf["adx"]
        macd_state = tf["macd_signal_state"]
        macd_cross = tf["macd_cross"]
        trend = tf["ema_trend_short"]
        # LONG signals
        if trend == "up": long_score += 1.0 * w
        if rsi < 35: long_score += 1.5 * w
        elif rsi < 45: long_score += 0.5 * w
        if bb < 20: long_score += 1.2 * w
        if ema_dist < -3: long_score += 1.0 * w
        if macd_cross == "bullish_cross": long_score += 1.5 * w
        if macd_state == "bullish" and trend == "up": long_score += 0.3 * w
        if adx > 25 and trend == "up": long_score += 0.5 * w
        # SHORT signals
        if trend == "down": short_score += 1.0 * w
        if rsi > 70: short_score += 1.5 * w
        elif rsi > 60: short_score += 0.5 * w
        if bb > 85: short_score += 1.2 * w
        if ema_dist > 5: short_score += 1.0 * w
        if macd_cross == "bearish_cross": short_score += 1.5 * w
        if macd_state == "bearish" and trend == "down": short_score += 0.3 * w
        if adx > 25 and trend == "down": short_score += 0.5 * w

    side = "LONG" if long_score > short_score else "SHORT"
    edge = abs(long_score - short_score)
    notes_lines = []
    for tf_name in ("4h", "1D"):
        tf = tfs.get(tf_name)
        if tf and tf.get("available"):
            notes_lines.append(
                f"{tf_name}:RSI{tf['rsi']:.0f} BB{tf['bb_position_pct']:.0f}% "
                f"EMA{tf['price_vs_ema20_pct']:+.1f}% ADX{tf['adx']:.0f}"
            )
    return side, long_score, short_score, " | ".join(notes_lines)


def main():
    print("\n=== TIER 1: Binance futures full scan ===\n")
    t0 = time.time()
    cands = tier1_scan(top_n=30)
    print(f"  Found {len(cands)} candidates in {time.time()-t0:.1f}s\n")
    for i, c in enumerate(cands[:30], 1):
        print(f"  {i:2}. {c['symbol']:14s} ch={c['ch24']:+6.2f}% vol=${c['vol_m']:6.0f}M  "
              f"rng={c['rng_pos']*100:3.0f}%  setup={c['setup']:30s} score={c['score']:.2f}")

    print("\n=== TIER 2: TV indicator pull for top 30 ===\n")
    t1 = time.time()
    results = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        future_map = {ex.submit(tier2_score_tv, c["symbol"]): c for c in cands}
        for fut in as_completed(future_map):
            c = future_map[fut]
            try:
                side, ls, ss, notes = fut.result(timeout=30)
                c["tv_side"] = side
                c["tv_long"] = ls
                c["tv_short"] = ss
                c["tv_edge"] = abs(ls - ss)
                c["tv_notes"] = notes
                results.append(c)
            except Exception as e:
                c["tv_side"] = None
                c["tv_edge"] = 0
                c["tv_notes"] = f"(err: {type(e).__name__})"
                results.append(c)
    print(f"  Pulled TV data for {len(results)} symbols in {time.time()-t1:.1f}s\n")

    # Final ranking: setup_score * (1 + tv_edge/10)
    for r in results:
        if r.get("tv_side"):
            r["final_score"] = r["score"] * (1 + r["tv_edge"] / 10)
        else:
            r["final_score"] = r["score"] * 0.5  # penalty no TV data
    results.sort(key=lambda x: x["final_score"], reverse=True)

    print("=== TIER 2 ranking (Binance setup + TV confluence) ===\n")
    for i, r in enumerate(results[:15], 1):
        side = r.get("tv_side", "?")
        edge = r.get("tv_edge", 0)
        bin_setup = r["setup"].replace("_LONG", "[L]").replace("_SHORT", "[S]").replace("_", " ")
        print(f"  {i:2}. {r['symbol']:14s} ch={r['ch24']:+6.2f}% vol={r['vol_m']:5.0f}M  "
              f"setup={bin_setup:24s}  TV={side or '-':6s} edge={edge:5.1f}  "
              f"final={r['final_score']:.2f}")
        if r.get("tv_notes"):
            print(f"      {r['tv_notes']}")

    # Save top 5 for tier 3
    top5 = results[:5]
    with open("state/deep_scan_top5.json", "w") as f:
        json.dump([{
            "symbol": x["symbol"], "price": x["price"], "ch24": x["ch24"],
            "vol_m": x["vol_m"], "setup": x["setup"],
            "tv_side": x.get("tv_side"), "tv_edge": x.get("tv_edge"),
            "tv_notes": x.get("tv_notes"), "final_score": x["final_score"]
        } for x in top5], f, indent=2)
    print(f"\n  Top 5 saved to state/deep_scan_top5.json -> ready for tier 3 pipeline")


if __name__ == "__main__":
    main()
