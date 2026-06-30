"""Check GMT squeeze + HYPE breakout zone live."""
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

for sym in ["GMTUSDT", "HYPEUSDT"]:
    print(f"\n=== {sym} ===")
    t = c.futures_ticker(symbol=sym)
    mark = float(c.futures_mark_price(symbol=sym)["markPrice"])
    hi = float(t['highPrice']); lo = float(t['lowPrice'])
    rng = (mark - lo) / (hi - lo) * 100
    print(f"Mark ${mark:.5f}  ch24 {t['priceChangePercent']}%")
    print(f"24h hi ${hi}  lo ${lo}  rng {rng:.0f}%")
    print(f"Vol ${float(t['quoteVolume'])/1e6:.0f}M")
    k = c.futures_klines(symbol=sym, interval="15m", limit=20)
    closes = [float(x[4]) for x in k]
    print(f"RSI15m: {rsi14(closes):.0f}")
    k1h = c.futures_klines(symbol=sym, interval="1h", limit=20)
    closes1h = [float(x[4]) for x in k1h]
    print(f"RSI1h: {rsi14(closes1h):.0f}")

    try:
        flow = requests.get("https://fapi.binance.com/futures/data/takerlongshortRatio",
            params={"symbol": sym, "period": "5m", "limit": 6}, timeout=5).json()
        print(f"5m flow last 6: {[f['buySellRatio'] for f in flow]}")
    except Exception as e: print(f"flow err: {e}")

    try:
        oi = requests.get("https://fapi.binance.com/futures/data/openInterestHist",
            params={"symbol": sym, "period": "1h", "limit": 24}, timeout=5).json()
        oi_24 = float(oi[0]['sumOpenInterest']); oi_now = float(oi[-1]['sumOpenInterest'])
        oi_3h = float(oi[-4]['sumOpenInterest']) if len(oi)>=4 else oi_now
        print(f"OI 24h: {(oi_now/oi_24-1)*100:+.1f}%  3h: {(oi_now/oi_3h-1)*100:+.1f}%")
    except Exception as e: print(f"OI err: {e}")

    fund = c.futures_funding_rate(symbol=sym, limit=3)
    print(f"Funding (latest 3 × 8h):")
    for f in fund:
        print(f"  {f['fundingRate']} ({float(f['fundingRate'])*100*3*365:+.0f}%/yr)")

    lev = c._request_futures_api("get", "leverageBracket", True, data={"symbol": sym})
    print(f"Max lev: {lev[0]['brackets'][0]['initialLeverage']}x")

    print(f"\nLast 6 × 15m candles:")
    for k_bar in k[-6:]:
        o = float(k_bar[1]); h = float(k_bar[2]); l = float(k_bar[3]); cl = float(k_bar[4])
        bar_pct = (cl - o) / o * 100
        print(f"  O:{o:.5f} H:{h:.5f} L:{l:.5f} C:{cl:.5f} ({bar_pct:+.2f}%)")
