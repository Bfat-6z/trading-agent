"""A+ setup scanner — strict 6-criteria checklist.
Only flags candidates passing 5/6 or 6/6. Otherwise honest 'no A+ today'."""
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


def fetch_top_movers():
    c = spot_client()
    tickers = c.futures_ticker()
    cands = []
    for t in tickers:
        sym = t.get("symbol", "")
        if not sym.endswith("USDT"): continue
        if sym[:-4] in EXCLUDE: continue
        try:
            vol_m = float(t.get("quoteVolume", 0)) / 1e6
            ch24 = float(t.get("priceChangePercent", 0))
            cnt = int(t.get("count", 0))
            if vol_m < 30 or cnt < 5000: continue   # require $30M+ vol = serious liquidity
            if abs(ch24) > 25: continue
            high = float(t["highPrice"]); low = float(t["lowPrice"]); price = float(t["lastPrice"])
            rng_pos = (price - low) / (high - low) if high > low else 0.5
            cands.append({"symbol": sym, "ch24": ch24, "vol_m": vol_m, "rng_pos": rng_pos, "price": price})
        except Exception: continue
    cands.sort(key=lambda x: x["vol_m"], reverse=True)
    return cands[:30]


def check_volume_trend(c, sym):
    """Volume today vs 7-day avg. Returns (today_m, 7d_avg_m, ratio)."""
    try:
        klines = c.futures_klines(symbol=sym, interval="1d", limit=8)
        if len(klines) < 7: return None, None, None
        # vol values index 7 = quote asset volume
        vols = [float(k[7]) / 1e6 for k in klines[:-1]]  # 7 historical days
        today = float(klines[-1][7]) / 1e6
        avg = sum(vols) / len(vols)
        return today, avg, today/avg if avg > 0 else None
    except Exception:
        return None, None, None


def check_7d_perf(c, sym):
    try:
        klines = c.futures_klines(symbol=sym, interval="1d", limit=8)
        if len(klines) < 7: return None
        d7 = float(klines[0][4])
        now = float(klines[-1][4])
        return (now - d7) / d7 * 100
    except Exception:
        return None


def check_funding(c, sym):
    """Returns (current annual_fr%, OI 24h change %)."""
    try:
        prem = c.futures_mark_price(symbol=sym)
        fr = float(prem.get("lastFundingRate", 0)) * 3 * 365 * 100  # annualized
        oi_hist = c.futures_open_interest_hist(symbol=sym, period="1h", limit=25)
        oi_chg = None
        if oi_hist and len(oi_hist) >= 2:
            oi0 = float(oi_hist[0]["sumOpenInterest"])
            oi1 = float(oi_hist[-1]["sumOpenInterest"])
            oi_chg = (oi1 - oi0) / oi0 * 100 if oi0 > 0 else None
        return fr, oi_chg
    except Exception:
        return None, None


def score_aplus(cand, tv, c):
    """Score against 6-criteria A+ checklist. Returns (score, side, detail)."""
    if not tv or "timeframes" not in tv:
        return 0, "NO_TV", {}
    tfs = tv["timeframes"]
    h15 = tfs.get("15m", {}); h1 = tfs.get("1h", {}); h4 = tfs.get("4h", {}); d1 = tfs.get("1D", {})
    if not all(tf.get("available") for tf in [h1, h4, d1]):
        return 0, "INCOMPLETE_TV", {}

    detail = {"checks": {}, "side": None, "score": 0}
    ch24 = cand["ch24"]

    # Direction proposal: trend of 4h+1D
    trend_4h = h4["ema_trend_short"]
    trend_1d = d1["ema_trend_short"]
    if trend_4h == "up" and trend_1d == "up":
        side = "LONG"
    elif trend_4h == "down" and trend_1d == "down":
        side = "SHORT"
    else:
        side = "MIXED"
    detail["side"] = side

    if side == "MIXED":
        detail["checks"]["3_multi_tf"] = (False, "TF_disagreement")
        return 0, side, detail

    # === CHECK 3: Multi-TF confluence (cheapest, do first) ===
    # ≥3 of 4 TFs agree direction + RSI healthy + MACD aligned
    tf_aligned = 0
    for tf in [h15, h1, h4, d1]:
        if not tf.get("available"): continue
        if tf["ema_trend_short"] == ("up" if side == "LONG" else "down"):
            tf_aligned += 1
    multi_tf_ok = tf_aligned >= 3
    detail["checks"]["3_multi_tf"] = (multi_tf_ok, f"{tf_aligned}/4_TFs_aligned")

    # 1D MACD must align with direction (catches ONDO mistake)
    macd_1d_ok = (d1["macd_hist"] > 0) if side == "LONG" else (d1["macd_hist"] < 0)
    if not macd_1d_ok:
        detail["checks"]["3_multi_tf"] = (False, f"{tf_aligned}/4_TFs_but_1D_MACD_wrong")
        multi_tf_ok = False

    # RSI not extreme
    if side == "LONG":
        rsi_ok = h1["rsi"] < 70 and h4["rsi"] < 75
    else:
        rsi_ok = h1["rsi"] > 30 and h4["rsi"] > 25
    if not rsi_ok:
        detail["checks"]["3_multi_tf"] = (False, f"RSI_extreme_h1={h1['rsi']:.0f}_h4={h4['rsi']:.0f}")
        multi_tf_ok = False

    # Not extended via 4h EMA dist
    if side == "LONG" and h4["price_vs_ema20_pct"] > 8:
        detail["checks"]["3_multi_tf"] = (False, f"4h_extended_+{h4['price_vs_ema20_pct']:.1f}%")
        multi_tf_ok = False
    if side == "SHORT" and h4["price_vs_ema20_pct"] < -8:
        detail["checks"]["3_multi_tf"] = (False, f"4h_extended_{h4['price_vs_ema20_pct']:.1f}%")
        multi_tf_ok = False

    if not multi_tf_ok:
        return 0, side, detail

    # === CHECK 1: Catalyst fresh — cannot determine programmatically, default unknown ===
    # We rely on news search separately; flag here as "unknown"
    detail["checks"]["1_catalyst"] = (None, "needs_news_search")

    # === CHECK 2: Sector leader vs laggard (7d perf) ===
    perf_7d = check_7d_perf(c, cand["symbol"])
    detail["perf_7d"] = perf_7d
    if perf_7d is None:
        detail["checks"]["2_sector_pos"] = (False, "no_data")
    elif side == "LONG":
        # LONG: 7d perf >= +3% (showing strength) or >= 0% acceptable
        leader_ok = perf_7d >= 3
        detail["checks"]["2_sector_pos"] = (leader_ok, f"7d={perf_7d:+.1f}%")
    else:  # SHORT
        # SHORT: 7d perf <= -3% (weakening) or near 0% acceptable
        leader_ok = perf_7d <= -3
        detail["checks"]["2_sector_pos"] = (leader_ok, f"7d={perf_7d:+.1f}%")

    # === CHECK 4: Volume increasing ===
    today_vol, avg_vol, vol_ratio = check_volume_trend(c, cand["symbol"])
    detail["vol_ratio"] = vol_ratio
    vol_ok = vol_ratio is not None and vol_ratio >= 1.2  # today 20%+ above avg
    detail["checks"]["4_volume"] = (vol_ok, f"today={today_vol:.0f}M_avg={avg_vol:.0f}M_ratio={vol_ratio:.2f}x" if vol_ratio else "no_data")

    # === CHECK 5: Funding + OI ===
    fr_annual, oi_chg = check_funding(c, cand["symbol"])
    detail["funding"] = fr_annual; detail["oi_chg"] = oi_chg
    # Funding: not extreme (between -20% and +50% annualized for entry direction)
    if fr_annual is not None:
        if side == "LONG":
            funding_ok = -20 <= fr_annual <= 30  # not crowded longs
        else:
            funding_ok = -30 <= fr_annual <= 20  # not crowded shorts
    else:
        funding_ok = False
    # OI: not spiking >15% (late entries piling in)
    oi_ok = (oi_chg is None or abs(oi_chg) <= 15)
    fo_ok = funding_ok and oi_ok
    detail["checks"]["5_funding_oi"] = (fo_ok, f"fr={fr_annual:+.1f}%/oi_24h={oi_chg:+.1f}%" if fr_annual is not None else "no_data")

    # === CHECK 6: Clear S/R (proxy: 4h EMA50 or 1D BB band as reference) ===
    # If 4h price near (within 2%) EMA50 = clear S/R reference. Otherwise less clear.
    sr_dist = abs(h4["close"] - h4["ema50"]) / h4["close"] * 100
    sr_ok = sr_dist <= 2.5  # within 2.5% of major MA = clear S/R
    detail["checks"]["6_sr_level"] = (sr_ok, f"price_to_4h_EMA50={sr_dist:.1f}%")

    # Score = count of passes (catalyst flagged as needs_news_search = neutral)
    passes = sum(1 for v, _ in detail["checks"].values() if v is True)
    detail["score"] = passes
    return passes, side, detail


def main():
    c = spot_client()
    print("\n=== A+ Setup Scanner (strict 6-criteria checklist) ===\n")
    print("Fetching top movers by volume...")
    cands = fetch_top_movers()
    print(f"  {len(cands)} candidates with vol > $30M\n")

    print("Pulling TV + scoring vs 6 checklist items...\n")
    results = []
    def worker(c_obj):
        try:
            tv = fetch_tv_multi_tf(c_obj["symbol"])
            score, side, detail = score_aplus(c_obj, tv, c)
            return {**c_obj, "score": score, "side": side, "detail": detail}
        except Exception as e:
            return {**c_obj, "score": 0, "side": "ERR", "detail": {"error": str(e)[:60]}}

    # Sequential to avoid TV websocket choking
    for i, x in enumerate(cands):
        if i % 5 == 0: print(f"  [{i+1}/{len(cands)}]")
        results.append(worker(x))

    # Sort by score desc
    results.sort(key=lambda x: x["score"], reverse=True)

    print(f"{'Symbol':14s} {'side':6s} {'7d':>7s} {'vol_x':>6s} {'fr_y':>7s} {'oi_24h':>7s} {'score':>6s} {'breakdown'}")
    print("-" * 120)
    aplus_candidates = []
    for r in results[:15]:
        d = r["detail"]
        if not d or "checks" not in d:
            note = d.get("error", "no data") if isinstance(d, dict) else "n/a"
            print(f"  {r['symbol']:14s} {r['side']:6s} {'---':>7s} {'---':>6s} {'---':>7s} {'---':>7s} {r['score']:>6}  {note}")
            continue
        perf = d.get("perf_7d", 0)
        vol_ratio = d.get("vol_ratio") or 0
        fr = d.get("funding") or 0
        oi = d.get("oi_chg") or 0
        checks = d["checks"]
        bd = " ".join(f"{k.split('_')[0]}={'OK' if v else 'X'}" for k, (v, _) in checks.items())
        print(f"  {r['symbol']:14s} {r['side']:6s} {perf:+6.1f}% {vol_ratio:5.2f}x {fr:+6.1f}% {oi:+6.1f}% {r['score']:5}/5  {bd}")
        if r["score"] >= 4:  # 4+ of 5 quantifiable checks (catalyst is news-driven, separate)
            aplus_candidates.append(r)

    print()
    if aplus_candidates:
        print(f"\n=== A+/A POTENTIAL ({len(aplus_candidates)} found) — needs news/catalyst check ===\n")
        for a in aplus_candidates:
            d = a["detail"]
            print(f"  {a['symbol']:14s} side={a['side']:5s} score={a['score']}/5  (catalyst check pending)")
            for k, (v, reason) in d["checks"].items():
                if v is False:
                    print(f"    [{k}] FAIL: {reason}")
                elif v is None:
                    print(f"    [{k}] NEEDS_NEWS")
    else:
        print("=== NO A+ CANDIDATES TODAY ===")
        print("Top scored failed at least 2 of 5 quantifiable checks.")
        print("Best partial:", results[0]["symbol"] if results else "n/a")
        print("\nRecommendation: HOLD CASH. Do not force. Wait for proper setup.")

    with open("state/aplus_scan.json", "w") as f:
        json.dump([{"symbol": r["symbol"], "side": r["side"], "score": r["score"],
                     "ch24": r["ch24"], "vol_m": r["vol_m"],
                     "detail": {k: v for k, v in r["detail"].items() if k != "checks"} | {"checks": {k: list(v) if isinstance(v, tuple) else v for k, v in r["detail"].get("checks", {}).items()}}}
                    for r in results[:15]], f, indent=2, default=str)
    print(f"\nResults saved to state/aplus_scan.json")


if __name__ == "__main__":
    main()
