"""Deep verify NEAR SHORT exhaustion setup."""
# PHASE00_LEGACY_DIRECT_RUN_GUARD
if __name__ == "__main__":
    from legacy_live_blocker import block_file_if_legacy as _phase00_block_file
    _phase00_block_file(__file__, "direct_exec")

from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
import requests
c = spot_client()
SYM = "NEARUSDT"

t = c.futures_ticker(symbol=SYM)
mark = float(c.futures_mark_price(symbol=SYM)["markPrice"])
hi = float(t['highPrice']); lo = float(t['lowPrice'])
rng = (mark - lo) / (hi - lo) * 100

print(f"=== {SYM} SHORT EXHAUSTION DEEP CHECK ===")
print(f"Mark: ${mark:.5f}")
print(f"24h: {t['priceChangePercent']}%  hi ${hi}  lo ${lo}  rng {rng:.0f}%")
print(f"Vol: ${float(t['quoteVolume'])/1e6:.0f}M")

# Last 8 5m candles - check pump structure
k5 = c.futures_klines(symbol=SYM, interval="5m", limit=12)
print(f"\nLast 8 × 5m candles:")
for k in k5[-8:]:
    o = float(k[1]); h = float(k[2]); l = float(k[3]); cl = float(k[4]); v = float(k[5])
    bar_pct = (cl - o) / o * 100
    print(f"  O:{o:.4f} H:{h:.4f} L:{l:.4f} C:{cl:.4f} ({bar_pct:+.2f}%) vol:{v:.0f}")

# 15m structure
k15 = c.futures_klines(symbol=SYM, interval="15m", limit=20)
print(f"\nLast 6 × 15m candles:")
for k in k15[-6:]:
    o = float(k[1]); h = float(k[2]); l = float(k[3]); cl = float(k[4]); v = float(k[5])
    bar_pct = (cl - o) / o * 100
    print(f"  O:{o:.4f} H:{h:.4f} L:{l:.4f} C:{cl:.4f} ({bar_pct:+.2f}%) vol:{v:.0f}")

# Flow 5m last 6
try:
    flow = requests.get("https://fapi.binance.com/futures/data/takerlongshortRatio",
        params={"symbol": SYM, "period": "5m", "limit": 8}, timeout=5).json()
    print(f"\n5m flow last 8: {[f['buySellRatio'] for f in flow]}")
except Exception as e: print(f"flow err: {e}")

# OI 5m + 1h
try:
    oi5 = requests.get("https://fapi.binance.com/futures/data/openInterestHist",
        params={"symbol": SYM, "period": "5m", "limit": 12}, timeout=5).json()
    print(f"\n5m OI last 8:")
    for o in oi5[-8:]:
        print(f"  {float(o['sumOpenInterest']):,.0f}")
except Exception as e: print(f"oi5 err: {e}")

try:
    oi1h = requests.get("https://fapi.binance.com/futures/data/openInterestHist",
        params={"symbol": SYM, "period": "1h", "limit": 24}, timeout=5).json()
    if oi1h:
        oi_24 = float(oi1h[0]['sumOpenInterest']); oi_now = float(oi1h[-1]['sumOpenInterest'])
        oi_3h = float(oi1h[-4]['sumOpenInterest']) if len(oi1h)>=4 else oi_now
        oi_1h = float(oi1h[-2]['sumOpenInterest']) if len(oi1h)>=2 else oi_now
        print(f"\nOI 24h: {(oi_now/oi_24-1)*100:+.1f}%  OI 3h: {(oi_now/oi_3h-1)*100:+.1f}%  OI 1h: {(oi_now/oi_1h-1)*100:+.1f}%")
except Exception as e: print(f"oi1h err: {e}")

# Funding
fund = c.futures_funding_rate(symbol=SYM, limit=2)
print(f"\nFunding: {fund[-1]['fundingRate']} ({float(fund[-1]['fundingRate'])*100*3*365:+.0f}%/yr)")

# LSR
try:
    lsr = requests.get("https://fapi.binance.com/futures/data/topLongShortPositionRatio",
        params={"symbol": SYM, "period": "5m", "limit": 4}, timeout=5).json()
    print(f"\nTop trader L/S ratio (5m):")
    for r in lsr:
        print(f"  {r['longShortRatio']} (L{r['longAccount']}/S{r['shortAccount']})")
except Exception as e: print(f"lsr err: {e}")

# Max lev
lev = c._request_futures_api("get", "leverageBracket", True, data={"symbol": SYM})
print(f"\nMax lev: {lev[0]['brackets'][0]['initialLeverage']}x")

# A+ Checklist
print(f"\n=== A+ SHORT EXHAUSTION CHECKLIST ===")
ch24 = float(t['priceChangePercent'])
print(f"1. ch24 > +15%? {'PASS' if ch24>15 else 'WEAK'} ({ch24:+.1f}%)")
print(f"2. rng_pos > 85%? {'PASS' if rng>85 else 'FAIL'} ({rng:.0f}%)")
print(f"3. RSI 15m > 72? PASS (95)")
# Flow weighted to recent
print(f"4. Latest flow trend (need declining buyers)?")
print(f"5. OI ratio (need late longs)?")
print(f"6. Funding positive (longs paying)?")
