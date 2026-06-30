"""Quick scan for best scalp candidate right now."""
# PHASE00_LEGACY_DIRECT_RUN_GUARD
if __name__ == "__main__":
    from legacy_live_blocker import block_file_if_legacy as _phase00_block_file
    _phase00_block_file(__file__, "direct_exec")

from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client

c = spot_client()
tickers = c.futures_ticker()
EXCL = {"USDC","FDUSD","TUSD","BUSD","DAI","USDP","EUR","XAU","XAG",
        "NVDA","TSLA","AAPL","MSFT","GOOGL","META","AMZN","INTC","AMD",
        "SOXL","MSTR","COIN","HOOD","RIOT"}

cands = []
for t in tickers:
    sym = t.get("symbol", "")
    if not sym.endswith("USDT") or sym[:-4] in EXCL:
        continue
    try:
        vol = float(t.get("quoteVolume", 0))/1e6
        ch = float(t.get("priceChangePercent", 0))
        if vol < 100 or abs(ch) > 50:
            continue
        high = float(t["highPrice"]); low = float(t["lowPrice"])
        price = float(t["lastPrice"])
        rng = (price-low)/(high-low) if high > low else 0.5
        # Score: prefer extreme rng (mean reversion likely)
        score = abs(rng - 0.5) * 2  # 0 to 1
        cands.append({
            "sym": sym, "ch": ch, "vol": vol, "rng": rng,
            "price": price, "score": score
        })
    except: continue

cands.sort(key=lambda x: x["score"], reverse=True)
print(f"{'Symbol':14s} {'ch24':>7s} {'vol $M':>9s} {'rng%':>5s} {'Side hint':>10s}")
for x in cands[:10]:
    side = "LONG" if x["rng"] < 0.2 else ("SHORT" if x["rng"] > 0.8 else "MID")
    print(f"  {x['sym']:14s} {x['ch']:+6.2f}% {x['vol']:>8.0f}M {x['rng']*100:>4.0f}% {side:>10s}")
print()
# For top 5 by extreme rng, fetch microstructure
TOP5 = cands[:5]
for x in TOP5:
    sym = x["sym"]
    try:
        prem = c.futures_mark_price(symbol=sym)
        fr = float(prem.get("lastFundingRate", 0))*100*3*365
        oi = c.futures_open_interest_hist(symbol=sym, period="1h", limit=25)
        oi_chg = (float(oi[-1]["sumOpenInterest"])-float(oi[0]["sumOpenInterest"]))/float(oi[0]["sumOpenInterest"])*100
        trades = c.futures_aggregate_trades(symbol=sym, limit=100)
        buy = sum(float(tt["q"]) for tt in trades if not tt["m"])
        sell = sum(float(tt["q"]) for tt in trades if tt["m"])
        ratio = buy/sell if sell > 0 else 999
        br = c._request_futures_api("get", "leverageBracket", True, data={"symbol": sym})
        max_lev = br[0]["brackets"][0]["initialLeverage"] if isinstance(br, list) and br else "?"
        print(f"{sym}: rng={x['rng']*100:.0f}% fr={fr:+.0f}% oi24={oi_chg:+.0f}% flow={ratio:.2f} maxLev={max_lev}x")
    except Exception as e:
        print(f"{sym}: ERR {str(e)[:40]}")
