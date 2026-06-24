"""Strict A+ scan: need 4-5/5 criteria."""
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
import requests
c = spot_client()

def rsi14(closes):
    if len(closes) < 15: return 50
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    avg_g = sum(gains[:14])/14; avg_l = sum(losses[:14])/14
    for i in range(14, len(gains)):
        avg_g = (avg_g*13 + gains[i]) / 14
        avg_l = (avg_l*13 + losses[i]) / 14
    rs = avg_g / avg_l if avg_l > 0 else 99
    return 100 - 100/(1+rs)

all_t = c.futures_ticker()
big = [t for t in all_t if float(t["quoteVolume"]) > 30_000_000 and t["symbol"].endswith("USDT")]

print(f"Scanning {len(big)} symbols for STRICT A+ (4+/5)...\n")

aplus_long = []
aplus_short = []

for t in big:
    sym = t["symbol"]
    try:
        ch24 = float(t["priceChangePercent"])
        hi = float(t["highPrice"]); lo = float(t["lowPrice"])
        last = float(t["lastPrice"])
        rng = (last - lo) / (hi - lo) * 100 if hi > lo else 50
        vol_m = float(t["quoteVolume"]) / 1e6

        # LONG bounce check (need 4/5)
        if ch24 < -5 and rng < 25:  # filter prefilter
            k = c.futures_klines(symbol=sym, interval="15m", limit=40)
            closes = [float(x[4]) for x in k]
            r = rsi14(closes)
            try:
                flow = requests.get("https://fapi.binance.com/futures/data/takerlongshortRatio",
                    params={"symbol": sym, "period": "15m", "limit": 2}, timeout=5).json()
                flow_now = float(flow[-1]["buySellRatio"])
            except: flow_now = 0
            try:
                oi = requests.get("https://fapi.binance.com/futures/data/openInterestHist",
                    params={"symbol": sym, "period": "15m", "limit": 8}, timeout=5).json()
                oi_now = float(oi[-1]['sumOpenInterest']); oi_2h = float(oi[0]['sumOpenInterest'])
                oi_change = (oi_now/oi_2h - 1)*100
            except: oi_change = 99
            fund = c.futures_funding_rate(symbol=sym, limit=1)
            f_pct = float(fund[0]['fundingRate'])*100*3*365 if fund else 0

            score = 0
            score += 1 if ch24 < -10 else 0
            score += 1 if rng < 20 else 0
            score += 1 if r < 35 else 0
            score += 1 if flow_now >= 1.3 else 0
            score += 1 if oi_change < 0 else 0
            score += 1 if -100 < f_pct < 50 else 0
            if score >= 4:
                aplus_long.append({"sym": sym, "score": score, "ch24": ch24, "rng": rng, "rsi": r, "flow": flow_now, "oi": oi_change, "fund": f_pct, "vol": vol_m})

        if ch24 > 15 and rng > 80:
            k = c.futures_klines(symbol=sym, interval="15m", limit=40)
            closes = [float(x[4]) for x in k]
            r = rsi14(closes)
            try:
                flow = requests.get("https://fapi.binance.com/futures/data/takerlongshortRatio",
                    params={"symbol": sym, "period": "15m", "limit": 2}, timeout=5).json()
                flow_now = float(flow[-1]["buySellRatio"])
            except: flow_now = 0
            try:
                oi = requests.get("https://fapi.binance.com/futures/data/openInterestHist",
                    params={"symbol": sym, "period": "15m", "limit": 8}, timeout=5).json()
                oi_now = float(oi[-1]['sumOpenInterest']); oi_2h = float(oi[0]['sumOpenInterest'])
                oi_change = (oi_now/oi_2h - 1)*100
            except: oi_change = -99
            fund = c.futures_funding_rate(symbol=sym, limit=1)
            f_pct = float(fund[0]['fundingRate'])*100*3*365 if fund else 0

            score = 0
            score += 1 if ch24 > 20 else 0
            score += 1 if rng > 85 else 0
            score += 1 if r > 75 else 0
            score += 1 if flow_now <= 0.85 else 0
            score += 1 if oi_change > 0 else 0
            score += 1 if -50 < f_pct < 100 else 0
            if score >= 4:
                aplus_short.append({"sym": sym, "score": score, "ch24": ch24, "rng": rng, "rsi": r, "flow": flow_now, "oi": oi_change, "fund": f_pct, "vol": vol_m})
    except Exception as e:
        pass

print(f"=== A+ LONG BOUNCE candidates (4+/6) ===")
aplus_long.sort(key=lambda x: -x["score"])
for c in aplus_long[:10]:
    print(f"  {c['sym']:14s} score={c['score']}/6 ch24={c['ch24']:+.1f}% rng={c['rng']:.0f}% rsi={c['rsi']:.0f} flow={c['flow']:.2f} oi2h={c['oi']:+.1f}% fund={c['fund']:+.0f}%/yr vol=${c['vol']:.0f}M")

print(f"\n=== A+ SHORT EXHAUSTION candidates (4+/6) ===")
aplus_short.sort(key=lambda x: -x["score"])
for c in aplus_short[:10]:
    print(f"  {c['sym']:14s} score={c['score']}/6 ch24={c['ch24']:+.1f}% rng={c['rng']:.0f}% rsi={c['rsi']:.0f} flow={c['flow']:.2f} oi2h={c['oi']:+.1f}% fund={c['fund']:+.0f}%/yr vol=${c['vol']:.0f}M")
