"""Count how many USDT pairs are tradable with various budgets."""
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
from tradingagents.binance import spot as bs

c = spot_client()
tickers = c.get_ticker()
usdt = [t for t in tickers if t["symbol"].endswith("USDT")]
print(f"Total USDT pairs on Binance: {len(usdt)}")

# Filter by vol + count
active = []
for t in usdt:
    try:
        v = float(t["quoteVolume"])
        cnt = int(t["count"])
        if v < 500_000 or cnt < 1000:
            continue
        active.append(t)
    except Exception:
        continue
print(f"Pairs with >=$500k vol AND >=1k trades: {len(active)}")

# Count by min_notional
buckets = {1.0: 0, 5.0: 0, 10.0: 0, 50.0: 0, "other": 0}
samples = {1.0: [], 5.0: [], 10.0: [], 50.0: []}
for t in active[:200]:    # sample top by volume
    try:
        f = bs.get_symbol_filters(t["symbol"])
        mn = float(f.get("min_notional", 0))
        if mn <= 1:
            buckets[1.0] += 1
            samples[1.0].append(t["symbol"])
        elif mn <= 5:
            buckets[5.0] += 1
            samples[5.0].append(t["symbol"])
        elif mn <= 10:
            buckets[10.0] += 1
            samples[10.0].append(t["symbol"])
        elif mn <= 50:
            buckets[50.0] += 1
            samples[50.0].append(t["symbol"])
        else:
            buckets["other"] += 1
    except Exception:
        continue

print("\nMin notional distribution (top 200 by volume):")
for k, v in buckets.items():
    print(f"  <= ${k}: {v} pairs")

print("\nSamples by bucket:")
for k in [1.0, 5.0]:
    print(f"  Bucket <= ${k}:")
    for s in samples[k][:20]:
        print(f"    {s}")
