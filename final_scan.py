from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
c = spot_client()
# Check top high-vol coins quickly for best directional setup
SYMS = ["BTCUSDT", "ETHUSDT", "SOLUSDT", "TONUSDT", "DOGEUSDT", "XRPUSDT",
        "LITUSDT", "PROVEUSDT", "ZECUSDT", "HYPEUSDT", "NEARUSDT", "ASTERUSDT"]
results = []
for sym in SYMS:
    try:
        t = c.futures_ticker(symbol=sym)
        mark = float(t["lastPrice"])
        h = float(t["highPrice"]); l = float(t["lowPrice"])
        rng = (mark-l)/(h-l)*100 if h > l else 50
        ch = float(t["priceChangePercent"])
        vol = float(t["quoteVolume"])/1e6
        prem = c.futures_mark_price(symbol=sym)
        fr = float(prem.get("lastFundingRate", 0))*100*3*365
        trades = c.futures_aggregate_trades(symbol=sym, limit=100)
        buy = sum(float(tt["q"]) for tt in trades if not tt["m"])
        sell = sum(float(tt["q"]) for tt in trades if tt["m"])
        ratio = buy/sell if sell > 0 else 999
        # Score: extreme rng + flow alignment
        long_score = 0
        short_score = 0
        if rng < 25 and ratio > 1.2:  # at low + buying = bounce
            long_score = (25 - rng) * ratio
        if rng > 75 and ratio < 0.85:  # at high + selling = fade
            short_score = (rng - 75) * (1/ratio)
        results.append((sym, mark, rng, ch, ratio, fr, vol, long_score, short_score))
    except Exception as e:
        pass
# Sort by best score
print("Best LONG bounces (rng<25 + buying flow):")
longs = sorted([r for r in results if r[7] > 0], key=lambda x: x[7], reverse=True)
for r in longs[:5]:
    print(f"  {r[0]:14s} ${r[1]} ch={r[3]:+.1f}% rng={r[2]:.0f}% flow={r[4]:.2f} fr={r[5]:+.0f}% vol=${r[6]:.0f}M")
print("\nBest SHORT fades (rng>75 + selling flow):")
shorts = sorted([r for r in results if r[8] > 0], key=lambda x: x[8], reverse=True)
for r in shorts[:5]:
    print(f"  {r[0]:14s} ${r[1]} ch={r[3]:+.1f}% rng={r[2]:.0f}% flow={r[4]:.2f} fr={r[5]:+.0f}% vol=${r[6]:.0f}M")
