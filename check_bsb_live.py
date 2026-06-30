"""Check BSB live state for trade decision."""
# PHASE00_LEGACY_DIRECT_RUN_GUARD
if __name__ == "__main__":
    from legacy_live_blocker import block_file_if_legacy as _phase00_block_file
    _phase00_block_file(__file__, "direct_exec")

from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
import requests
c = spot_client()
SYM = "BSBUSDT"

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

t = c.futures_ticker(symbol=SYM)
mark = float(c.futures_mark_price(symbol=SYM)["markPrice"])
hi = float(t['highPrice']); lo = float(t['lowPrice'])
rng = (mark - lo) / (hi - lo) * 100

print(f"=== {SYM} LIVE STATE ===")
print(f"Mark ${mark:.5f}  ch24 {t['priceChangePercent']}%")
print(f"24h hi ${hi}  lo ${lo}  rng {rng:.0f}%")
print(f"Vol ${float(t['quoteVolume'])/1e6:.0f}M")

k15 = c.futures_klines(symbol=SYM, interval="15m", limit=20)
closes = [float(x[4]) for x in k15]
print(f"RSI15m: {rsi14(closes):.0f}")
k1h = c.futures_klines(symbol=SYM, interval="1h", limit=20)
closes1h = [float(x[4]) for x in k1h]
print(f"RSI1h: {rsi14(closes1h):.0f}")

print(f"\nLast 6 × 15m candles:")
for k in k15[-6:]:
    o=float(k[1]); h=float(k[2]); l=float(k[3]); cl=float(k[4]); v=float(k[5])
    pct = (cl-o)/o*100
    print(f"  O:{o:.4f} H:{h:.4f} L:{l:.4f} C:{cl:.4f} ({pct:+.2f}%) vol:{v:.0f}")

print(f"\nLast 4 × 5m candles:")
k5 = c.futures_klines(symbol=SYM, interval="5m", limit=8)
for k in k5[-4:]:
    o=float(k[1]); h=float(k[2]); l=float(k[3]); cl=float(k[4])
    pct = (cl-o)/o*100
    print(f"  O:{o:.4f} H:{h:.4f} L:{l:.4f} C:{cl:.4f} ({pct:+.2f}%)")

try:
    flow = requests.get("https://fapi.binance.com/futures/data/takerlongshortRatio",
        params={"symbol": SYM, "period": "15m", "limit": 6}, timeout=5).json()
    print(f"\n15m flow last 6: {[f['buySellRatio'] for f in flow]}")
except Exception as e: print(f"flow err: {e}")

try:
    flow5 = requests.get("https://fapi.binance.com/futures/data/takerlongshortRatio",
        params={"symbol": SYM, "period": "5m", "limit": 6}, timeout=5).json()
    print(f"5m flow last 6: {[f['buySellRatio'] for f in flow5]}")
except: pass

try:
    oi = requests.get("https://fapi.binance.com/futures/data/openInterestHist",
        params={"symbol": SYM, "period": "1h", "limit": 24}, timeout=5).json()
    oi_24 = float(oi[0]['sumOpenInterest']); oi_now = float(oi[-1]['sumOpenInterest'])
    oi_3h = float(oi[-4]['sumOpenInterest']) if len(oi)>=4 else oi_now
    oi_1h = float(oi[-2]['sumOpenInterest']) if len(oi)>=2 else oi_now
    print(f"\nOI 24h: {(oi_now/oi_24-1)*100:+.1f}%  3h: {(oi_now/oi_3h-1)*100:+.1f}%  1h: {(oi_now/oi_1h-1)*100:+.1f}%")
except Exception as e: print(f"OI err: {e}")

fund = c.futures_funding_rate(symbol=SYM, limit=3)
print(f"\nFunding last 3 × 8h:")
for f in fund:
    print(f"  {f['fundingRate']} ({float(f['fundingRate'])*100*3*365:+.0f}%/yr)")

try:
    lsr = requests.get("https://fapi.binance.com/futures/data/topLongShortPositionRatio",
        params={"symbol": SYM, "period": "15m", "limit": 4}, timeout=5).json()
    print(f"\nTop trader L/S (15m last 4):")
    for r in lsr:
        print(f"  {r['longShortRatio']} L{r['longAccount']}/S{r['shortAccount']}")
except: pass

# Max lev + tick
lev = c._request_futures_api("get", "leverageBracket", True, data={"symbol": SYM})
print(f"\nMax lev: {lev[0]['brackets'][0]['initialLeverage']}x")

# VWAP
vols = [float(k[5]) for k in k15]
pvs = [(float(k[2])+float(k[3])+float(k[4]))/3 * float(k[5]) for k in k15]
vwap = sum(pvs)/sum(vols) if sum(vols)>0 else mark
print(f"VWAP (20×15m): ${vwap:.5f}  Mark vs VWAP: {(mark/vwap-1)*100:+.2f}%")
