# PHASE00_LEGACY_DIRECT_RUN_GUARD
if __name__ == "__main__":
    from legacy_live_blocker import block_file_if_legacy as _phase00_block_file
    _phase00_block_file(__file__, "direct_exec")

from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client

c = spot_client()
for sym in ["LITUSDT", "ZECUSDT", "PROVEUSDT", "TONUSDT"]:
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
        t = c.futures_ticker(symbol=sym)
        mark = float(t["lastPrice"])
        h = float(t["highPrice"]); l = float(t["lowPrice"])
        rng = (mark-l)/(h-l)*100 if h>l else 50
        ch = float(t["priceChangePercent"])
        print(f"{sym}: ${mark} ch={ch:+.1f}% rng={rng:.0f}% fr={fr:+.0f}% oi24={oi_chg:+.0f}% flow={ratio:.2f} maxLev={max_lev}x")
    except Exception as e:
        print(f"{sym}: ERR {str(e)[:40]}")
