"""Pre-trade verification: runs the 12-step checklist on a specific symbol+direction.
Usage: python verify_trade.py SYMBOL LONG|SHORT [margin_usd] [leverage]
Exit code 0 = all PASS, 1 = at least 1 FAIL.

Reference: C:\\Users\\ACER\\.claude\\projects\\E--keo-moi-mail\\memory\\reference_pre_trade_checklist.md
"""
import sys
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
from tradingagents.crypto.tv_data import fetch_tv_multi_tf


def main():
    if len(sys.argv) < 3:
        print("Usage: verify_trade.py SYMBOL LONG|SHORT [margin_usd=1.5] [leverage=5]")
        sys.exit(2)
    sym = sys.argv[1].upper()
    side = sys.argv[2].upper()
    margin = float(sys.argv[3]) if len(sys.argv) > 3 else 1.5
    lev = int(sys.argv[4]) if len(sys.argv) > 4 else 5
    if side not in ("LONG", "SHORT"):
        print(f"Side must be LONG or SHORT, got {side}")
        sys.exit(2)
    notional = margin * lev

    c = spot_client()
    results = []  # list of (item, status, detail)

    print(f"\n=== PRE-TRADE CHECKLIST {sym} {side} ===")
    print(f"Margin: ${margin}  Leverage: {lev}x  Notional: ${notional}\n")

    # === Tier data ===
    t = c.futures_ticker(symbol=sym)
    ch24 = float(t["priceChangePercent"])
    vol_m = float(t["quoteVolume"]) / 1e6
    high24 = float(t["highPrice"])
    low24 = float(t["lowPrice"])
    price = float(t["lastPrice"])
    rng_pos = (price - low24) / (high24 - low24) if high24 > low24 else 0.5

    # === [1] Catalyst — flagged for manual ===
    results.append(("1_catalyst", "MANUAL",
        f"User must verify <48h token-specific catalyst via news search"))

    # === [2] Sector leader — requires manual peer list, default skip ===
    results.append(("2_sector_leader", "MANUAL",
        f"User must list 3-5 peer symbols + compare 7d perf"))

    # === [3] Volume vs 7d avg ===
    try:
        klines = c.futures_klines(symbol=sym, interval="1d", limit=8)
        vols = [float(k[7]) / 1e6 for k in klines[:-1]]
        today_vol = float(klines[-1][7]) / 1e6
        avg_vol = sum(vols) / len(vols) if vols else 0
        ratio = today_vol / avg_vol if avg_vol > 0 else 0
        status = "PASS" if ratio >= 0.8 else "FAIL"
        results.append(("3_volume", status, f"today=${today_vol:.0f}M avg=${avg_vol:.0f}M ratio={ratio:.2f}x (need >=0.8)"))
    except Exception as e:
        results.append(("3_volume", "FAIL", f"data error: {e}"))

    # === [4-6] TV multi-TF ===
    tv = fetch_tv_multi_tf(sym)
    if not tv or "timeframes" not in tv:
        results.append(("4_multi_tf", "FAIL", "TV data unavailable"))
        results.append(("5_1d_macd", "FAIL", "TV data unavailable"))
        results.append(("6_not_extended", "FAIL", "TV data unavailable"))
    else:
        tfs = tv["timeframes"]
        h15 = tfs.get("15m", {}); h1 = tfs.get("1h", {}); h4 = tfs.get("4h", {}); d1 = tfs.get("1D", {})

        # [4] Multi-TF confluence
        want = "up" if side == "LONG" else "down"
        aligned = sum(1 for tf in [h15, h1, h4, d1]
                      if tf.get("available") and tf.get("ema_trend_short") == want)
        status = "PASS" if aligned >= 3 else "FAIL"
        results.append(("4_multi_tf", status,
            f"{aligned}/4 TFs trend={want} (15m={h15.get('ema_trend_short')} 1h={h1.get('ema_trend_short')} 4h={h4.get('ema_trend_short')} 1D={d1.get('ema_trend_short')})"))

        # [5] 1D MACD aligned
        if d1.get("available"):
            macd = d1.get("macd_hist", 0)
            macd_ok = (macd > 0) if side == "LONG" else (macd < 0)
            results.append(("5_1d_macd", "PASS" if macd_ok else "FAIL", f"1D MACD hist={macd:+.4f}"))
        else:
            results.append(("5_1d_macd", "FAIL", "1D unavailable"))

        # [6] Not extended (4h EMA distance)
        if h4.get("available"):
            ema_dist = h4.get("price_vs_ema20_pct", 0)
            if side == "LONG":
                ext_ok = ema_dist <= 8
            else:
                ext_ok = ema_dist >= -8
            results.append(("6_not_extended", "PASS" if ext_ok else "FAIL", f"4h_ema_dist={ema_dist:+.1f}% (LONG<=8, SHORT>=-8)"))
        else:
            results.append(("6_not_extended", "FAIL", "4h unavailable"))

    # === [7] Funding rate ===
    try:
        prem = c.futures_mark_price(symbol=sym)
        fr_8h = float(prem.get("lastFundingRate", 0)) * 100
        fr_annual = fr_8h * 3 * 365
        if side == "LONG":
            funding_ok = -20 <= fr_annual <= 50
            range_str = "(-20 to +50)"
        else:
            funding_ok = -10 <= fr_annual <= 30
            range_str = "(-10 to +30)"
        results.append(("7_funding", "PASS" if funding_ok else "FAIL",
            f"annual={fr_annual:+.1f}% need {range_str}"))
    except Exception as e:
        results.append(("7_funding", "FAIL", f"err: {e}"))

    # === [8] OI 24h ===
    try:
        oi_hist = c.futures_open_interest_hist(symbol=sym, period="1h", limit=25)
        if oi_hist and len(oi_hist) >= 2:
            oi0 = float(oi_hist[0]["sumOpenInterest"])
            oi_now = float(oi_hist[-1]["sumOpenInterest"])
            oi_chg = (oi_now - oi0) / oi0 * 100 if oi0 > 0 else 0
            if side == "LONG":
                oi_ok = oi_chg >= -5
                range_str = ">=-5%"
            else:
                oi_ok = oi_chg <= 15
                range_str = "<=+15%"
            results.append(("8_oi", "PASS" if oi_ok else "FAIL",
                f"oi_24h_chg={oi_chg:+.1f}% need {range_str}"))
        else:
            results.append(("8_oi", "FAIL", "insufficient OI data"))
    except Exception as e:
        results.append(("8_oi", "FAIL", f"err: {e}"))

    # === [9] Trade flow ===
    try:
        trades = c.futures_aggregate_trades(symbol=sym, limit=100)
        buy_vol = sum(float(t["q"]) for t in trades if not t["m"])
        sell_vol = sum(float(t["q"]) for t in trades if t["m"])
        ratio = buy_vol / sell_vol if sell_vol > 0 else 999
        if side == "LONG":
            flow_ok = ratio >= 0.85
            note = f"buy/sell={ratio:.2f} need >=0.85"
        else:
            flow_ok = ratio <= 1.15
            note = f"buy/sell={ratio:.2f} need <=1.15"
        results.append(("9_trade_flow", "PASS" if flow_ok else "FAIL", note))
    except Exception as e:
        results.append(("9_trade_flow", "FAIL", f"err: {e}"))

    # === [10] Entry timing — not at extreme ===
    if side == "LONG":
        timing_ok = rng_pos < 0.85
        note = f"rng_pos={rng_pos*100:.0f}% need <85%"
    else:
        timing_ok = rng_pos > 0.15
        note = f"rng_pos={rng_pos*100:.0f}% need >15%"
    results.append(("10_timing", "PASS" if timing_ok else "FAIL", note))

    # === [11] R:R math ===
    # Standard: SL 5%, TP 10%
    sl_loss = 0.05 * notional
    tp_gain = 0.10 * notional
    results.append(("11_rr_math", "INFO",
        f"5% SL loss=${sl_loss:.2f}  10% TP gain=${tp_gain:.2f}  Notional=${notional}"))

    # === [12] User rule alignment ===
    results.append(("12_user_rules", "MANUAL",
        f"User said target=$1 per trade. TP gain ${tp_gain:.2f} >= target? {'YES' if tp_gain >= 1 else 'NO'}"))

    # === Print results ===
    print(f"{'Item':22s} {'Status':8s} {'Detail'}")
    print("-" * 100)
    fail_count = 0
    for item, status, detail in results:
        marker = "v" if status == "PASS" else ("x" if status == "FAIL" else "?")
        print(f"  {item:20s} [{status:6s}] {detail}")
        if status == "FAIL":
            fail_count += 1

    print()
    print(f"=== TOTAL FAILS: {fail_count} ===")
    manual_count = sum(1 for _, s, _ in results if s == "MANUAL")
    print(f"=== MANUAL items: {manual_count} (catalyst, sector peers, user rules) ===")
    if fail_count == 0:
        print("[OK] VERDICT: All quantifiable checks PASS. Verify MANUAL items then OK to proceed.")
    elif fail_count == 1:
        print("[WARN] VERDICT: 1 FAIL. Proceed ONLY with explicit user approval naming that fail.")
    else:
        print(f"[STOP] VERDICT: {fail_count} FAILS. DO NOT TRADE. Skip this setup.")

    sys.exit(0 if fail_count == 0 else 1)


if __name__ == "__main__":
    main()
