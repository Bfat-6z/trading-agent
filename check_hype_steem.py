"""Check HYPE + STEEM live deeper."""
# PHASE00_LEGACY_DIRECT_RUN_GUARD
if __name__ == "__main__":
    from legacy_live_blocker import block_file_if_legacy as _phase00_block_file
    _phase00_block_file(__file__, "direct_exec")

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

for sym in ["HYPEUSDT", "STEEMUSDT", "TAOUSDT", "BILLUSDT", "ENAUSDT"]:
    print(f"\n=== {sym} ===")
    try:
        t = c.futures_ticker(symbol=sym)
        mark = float(c.futures_mark_price(symbol=sym)["markPrice"])
        hi = float(t['highPrice']); lo = float(t['lowPrice'])
        rng = (mark - lo) / (hi - lo) * 100 if hi > lo else 50
        ch24 = float(t['priceChangePercent'])
        vol_m = float(t['quoteVolume']) / 1e6

        k15 = c.futures_klines(symbol=sym, interval="15m", limit=20)
        closes15 = [float(x[4]) for x in k15]
        r15 = rsi14(closes15)
        k1h = c.futures_klines(symbol=sym, interval="1h", limit=20)
        closes1h = [float(x[4]) for x in k1h]
        r1h = rsi14(closes1h)

        print(f"Mark ${mark:.5f} ch24={ch24:+.1f}% rng={rng:.0f}% vol=${vol_m:.0f}M")
        print(f"24h hi ${hi:.5f}  lo ${lo:.5f}")
        print(f"RSI 15m: {r15:.0f}  1h: {r1h:.0f}")

        try:
            flow = requests.get("https://fapi.binance.com/futures/data/takerlongshortRatio",
                params={"symbol": sym, "period": "15m", "limit": 4}, timeout=5).json()
            print(f"15m flow last 4: {' '.join(f['buySellRatio'] for f in flow)}")
        except Exception as e: print(f"flow err: {e}")

        try:
            oi = requests.get("https://fapi.binance.com/futures/data/openInterestHist",
                params={"symbol": sym, "period": "1h", "limit": 24}, timeout=5).json()
            if oi:
                oi_24 = float(oi[0]['sumOpenInterest']); oi_now = float(oi[-1]['sumOpenInterest'])
                oi_2h = float(oi[-3]['sumOpenInterest']) if len(oi)>=3 else oi_now
                print(f"OI 24h: {(oi_now/oi_24-1)*100:+.1f}%  OI 2h: {(oi_now/oi_2h-1)*100:+.1f}%")
        except Exception as e: print(f"OI err: {e}")

        try:
            fund = c.futures_funding_rate(symbol=sym, limit=1)
            f_pct = float(fund[0]['fundingRate'])*100*3*365 if fund else 0
            print(f"Funding: {f_pct:+.0f}%/yr  ({fund[0]['fundingRate']})")
        except Exception as e: print(f"fund err: {e}")

        try:
            lev = c._request_futures_api("get", "leverageBracket", True, data={"symbol": sym})
            print(f"Max lev: {lev[0]['brackets'][0]['initialLeverage']}x")
        except Exception as e: print(f"lev err: {e}")
    except Exception as e:
        print(f"err: {e}")
