"""A+ 5-criteria scan — LONG bounce or SHORT exhaustion.
User-spec exact:
LONG: ch24<-5%, rng_pos<20%, 15m RSI<40, taker buy/sell latest 15m>=1.5, OI 24h NEG
SHORT: ch24>+15%, rng_pos>85%, 15m RSI>72, taker<=0.7, OI 24h POS +10%+
"""
from dotenv import load_dotenv
load_dotenv()
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from tradingagents.binance.client import spot_client

from universe_filter import NON_CRYPTO as EXCLUDE   # canonical shared stock/commodity exclusion

MAJORS = ["BTC","ETH","SOL","BNB","HYPE","NEAR","XRP","ADA","LINK","DOT","AVAX"]


def rsi(closes, n=14):
    if len(closes) < n + 1: return None
    gains = [max(closes[i]-closes[i-1], 0) for i in range(1, len(closes))]
    losses = [max(closes[i-1]-closes[i], 0) for i in range(1, len(closes))]
    avg_g = sum(gains[:n])/n
    avg_l = sum(losses[:n])/n
    for i in range(n, len(gains)):
        avg_g = (avg_g*(n-1) + gains[i])/n
        avg_l = (avg_l*(n-1) + losses[i])/n
    if avg_l == 0: return 100
    rs = avg_g/avg_l
    return 100 - 100/(1+rs)


def analyze(c, sym):
    try:
        # 24h ticker
        t = c.futures_ticker(symbol=sym)
        ch24 = float(t["priceChangePercent"])
        high = float(t["highPrice"]); low = float(t["lowPrice"]); price = float(t["lastPrice"])
        vol_m = float(t["quoteVolume"]) / 1e6
        rng_pos = (price - low)/(high - low) * 100 if high > low else 50
        # 15m klines for RSI
        kl = c.futures_klines(symbol=sym, interval="15m", limit=50)
        closes = [float(k[4]) for k in kl]
        r15 = rsi(closes, 14)
        # Latest 15m taker flow buy/sell
        taker = c.futures_aggregate_trades(symbol=sym, limit=1000)
        # use buy_vol/sell_vol ratio via klines taker buy quote vol
        last_kl = kl[-1]
        taker_buy_quote = float(last_kl[10])
        total_quote = float(last_kl[7])
        sell_quote = total_quote - taker_buy_quote
        tflow = taker_buy_quote / sell_quote if sell_quote > 0 else 99
        # OI 24h trend
        oi_hist = c.futures_open_interest_hist(symbol=sym, period="1h", limit=25)
        oi_chg = None
        if oi_hist and len(oi_hist) >= 2:
            oi0 = float(oi_hist[0]["sumOpenInterest"])
            oi1 = float(oi_hist[-1]["sumOpenInterest"])
            oi_chg = (oi1-oi0)/oi0*100 if oi0 > 0 else None
        return {
            "symbol": sym, "ch24": ch24, "rng_pos": rng_pos, "rsi15": r15,
            "tflow": tflow, "oi_chg": oi_chg, "price": price, "vol_m": vol_m
        }
    except Exception as e:
        return {"symbol": sym, "error": str(e)[:80]}


def score_long(a):
    if "error" in a: return 0, []
    c = []
    c.append(("ch24<-5%", a["ch24"] < -5, f"{a['ch24']:+.1f}%"))
    c.append(("rng_pos<20%", a["rng_pos"] < 20, f"{a['rng_pos']:.0f}%"))
    c.append(("RSI15<40", a["rsi15"] is not None and a["rsi15"] < 40, f"{a['rsi15']:.0f}" if a["rsi15"] else "n/a"))
    c.append(("tflow>=1.5", a["tflow"] >= 1.5, f"{a['tflow']:.2f}"))
    c.append(("OI24h<0", a["oi_chg"] is not None and a["oi_chg"] < 0, f"{a['oi_chg']:+.1f}%" if a["oi_chg"] is not None else "n/a"))
    return sum(1 for _, ok, _ in c if ok), c


def score_short(a):
    if "error" in a: return 0, []
    c = []
    c.append(("ch24>+15%", a["ch24"] > 15, f"{a['ch24']:+.1f}%"))
    c.append(("rng_pos>85%", a["rng_pos"] > 85, f"{a['rng_pos']:.0f}%"))
    c.append(("RSI15>72", a["rsi15"] is not None and a["rsi15"] > 72, f"{a['rsi15']:.0f}" if a["rsi15"] else "n/a"))
    c.append(("tflow<=0.7", a["tflow"] <= 0.7, f"{a['tflow']:.2f}"))
    c.append(("OI24h>+10%", a["oi_chg"] is not None and a["oi_chg"] > 10, f"{a['oi_chg']:+.1f}%" if a["oi_chg"] is not None else "n/a"))
    return sum(1 for _, ok, _ in c if ok), c


def main():
    c = spot_client()
    tickers = c.futures_ticker()
    # Build universe
    valid = []
    for t in tickers:
        s = t.get("symbol", "")
        if not s.endswith("USDT"): continue
        if s[:-4] in EXCLUDE: continue
        try:
            ch = float(t["priceChangePercent"])
            vol = float(t["quoteVolume"]) / 1e6
            if vol < 30: continue  # require liquidity
        except: continue
        valid.append((s, ch, vol))

    # Majors + top10 gainers + top10 losers
    universe = set(m+"USDT" for m in MAJORS)
    sorted_by_ch = sorted(valid, key=lambda x: x[1])
    universe.update([s for s, _, _ in sorted_by_ch[:10]])  # biggest losers
    universe.update([s for s, _, _ in sorted_by_ch[-10:]])  # biggest gainers
    universe = sorted(universe)
    print(f"Scanning {len(universe)} symbols: {', '.join(s.replace('USDT','') for s in universe)}\n")

    results = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(analyze, c, s): s for s in universe}
        for f in as_completed(futs):
            results.append(f.result())

    long_cands = []
    short_cands = []
    print(f"{'Sym':10s} {'ch24':>7s} {'rng%':>5s} {'RSI15':>5s} {'tflow':>6s} {'OI24h':>7s}  LONG  SHORT")
    print("-"*78)
    for a in sorted(results, key=lambda x: x.get("symbol","")):
        if "error" in a:
            print(f"{a['symbol']:10s} ERR: {a['error']}")
            continue
        ls, ld = score_long(a)
        ss, sd = score_short(a)
        print(f"{a['symbol']:10s} {a['ch24']:+6.1f}% {a['rng_pos']:4.0f}% {a['rsi15'] or 0:5.0f} {a['tflow']:6.2f} {a['oi_chg'] or 0:+6.1f}%   {ls}/5   {ss}/5")
        if ls >= 4: long_cands.append((a, ls, ld))
        if ss >= 4: short_cands.append((a, ss, sd))

    print("\n=== CANDIDATES (4+/5) ===\n")
    if not long_cands and not short_cands:
        print("NONE. No 4+/5 setups on this scan.")
    for a, sc, detail in long_cands:
        print(f"LONG {a['symbol']} score={sc}/5  vol=${a['vol_m']:.0f}M  price={a['price']}")
        for name, ok, val in detail:
            print(f"   [{'OK' if ok else 'X '}] {name:14s} = {val}")
    for a, sc, detail in short_cands:
        print(f"SHORT {a['symbol']} score={sc}/5  vol=${a['vol_m']:.0f}M  price={a['price']}")
        for name, ok, val in detail:
            print(f"   [{'OK' if ok else 'X '}] {name:14s} = {val}")


if __name__ == "__main__":
    main()
