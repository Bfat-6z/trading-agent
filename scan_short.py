"""Scan for SHORT setups: pumped coins at top of range."""
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
from tradingagents.binance import spot as bs

c = spot_client()
tickers = c.futures_ticker()
short_candidates = []
EXCLUDE = {"USDC", "FDUSD", "TUSD", "BUSD", "DAI", "USDP", "EUR", "USD1"}

for t in tickers:
    sym = t.get("symbol", "")
    if not sym.endswith("USDT"):
        continue
    base = sym[:-4]
    if base in EXCLUDE or base.endswith(("UP", "DOWN", "BULL", "BEAR")):
        continue
    try:
        vol_m = float(t.get("quoteVolume", 0)) / 1e6
        ch = float(t.get("priceChangePercent", 0))
        cnt = int(t.get("count", 0))
        if vol_m < 10 or cnt < 5000:
            continue
        # SHORT setup: pumped + near top of range
        if not (8 <= ch <= 25):
            continue
        high = float(t["highPrice"])
        low = float(t["lowPrice"])
        price = float(t["lastPrice"])
        rng_pos = (price - low) / (high - low) if high > low else 0.5
        if rng_pos < 0.7:    # not at top yet, may continue up
            continue
        # Distance from current to 24h high (how much room to dump)
        dump_room_pct = (price - low) / price * 100
        short_candidates.append({
            "symbol": sym, "ch24": ch, "vol_m": vol_m,
            "price": price, "rng_pos": rng_pos, "dump_room_pct": dump_room_pct,
        })
    except Exception:
        continue

short_candidates.sort(key=lambda x: x["ch24"], reverse=True)
print(f"SHORT candidates (pumped + at top of range):")
print(f"{'Symbol':<14} {'ch24':<8} {'vol_M':<8} {'rngPos':<8} {'dumpRoom':<10}")
for c_ in short_candidates[:15]:
    print(f"{c_['symbol']:<14} {c_['ch24']:+.2f}%  {c_['vol_m']:<8.0f} {c_['rng_pos']:.2f}     {c_['dump_room_pct']:.1f}%")
