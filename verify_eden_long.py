"""Deep verify EDEN A+ LONG bounce setup."""
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
import requests
c = spot_client()
SYM = "EDENUSDT"

t = c.futures_ticker(symbol=SYM)
mark = float(c.futures_mark_price(symbol=SYM)["markPrice"])
hi = float(t['highPrice']); lo = float(t['lowPrice'])
rng = (mark - lo) / (hi - lo) * 100

print(f"=== {SYM} A+ LONG BOUNCE VERIFICATION ===")
print(f"Mark ${mark:.5f}  ch24 {t['priceChangePercent']}%")
print(f"24h hi ${hi}  lo ${lo}  rng {rng:.0f}%")
print(f"Vol ${float(t['quoteVolume'])/1e6:.0f}M")
print(f"Distance from low: {(mark/lo-1)*100:+.2f}%")

# Last 8 5m candles - momentum
k5 = c.futures_klines(symbol=SYM, interval="5m", limit=12)
print(f"\nLast 8 × 5m candles:")
for k in k5[-8:]:
    o = float(k[1]); h = float(k[2]); l = float(k[3]); cl = float(k[4]); v = float(k[5])
    bar_pct = (cl - o) / o * 100
    print(f"  O:{o:.5f} H:{h:.5f} L:{l:.5f} C:{cl:.5f} ({bar_pct:+.2f}%) vol:{v:.0f}")

# 15m structure
k15 = c.futures_klines(symbol=SYM, interval="15m", limit=20)
print(f"\nLast 6 × 15m candles:")
for k in k15[-6:]:
    o = float(k[1]); h = float(k[2]); l = float(k[3]); cl = float(k[4]); v = float(k[5])
    bar_pct = (cl - o) / o * 100
    print(f"  O:{o:.5f} H:{h:.5f} L:{l:.5f} C:{cl:.5f} ({bar_pct:+.2f}%) vol:{v:.0f}")

# Flow 5m last 8
try:
    flow = requests.get("https://fapi.binance.com/futures/data/takerlongshortRatio",
        params={"symbol": SYM, "period": "5m", "limit": 8}, timeout=5).json()
    print(f"\n5m flow last 8: {[f['buySellRatio'] for f in flow]}")
except Exception as e: print(f"flow err: {e}")

# 15m flow trend
try:
    flow15 = requests.get("https://fapi.binance.com/futures/data/takerlongshortRatio",
        params={"symbol": SYM, "period": "15m", "limit": 6}, timeout=5).json()
    print(f"15m flow last 6: {[f['buySellRatio'] for f in flow15]}")
except: pass

# OI history
try:
    oi = requests.get("https://fapi.binance.com/futures/data/openInterestHist",
        params={"symbol": SYM, "period": "1h", "limit": 24}, timeout=5).json()
    oi_24 = float(oi[0]['sumOpenInterest']); oi_now = float(oi[-1]['sumOpenInterest'])
    oi_2h = float(oi[-3]['sumOpenInterest']) if len(oi)>=3 else oi_now
    oi_4h = float(oi[-5]['sumOpenInterest']) if len(oi)>=5 else oi_now
    print(f"\nOI 24h: {(oi_now/oi_24-1)*100:+.1f}%  4h: {(oi_now/oi_4h-1)*100:+.1f}%  2h: {(oi_now/oi_2h-1)*100:+.1f}%")
except Exception as e: print(f"oi err: {e}")

# Top trader L/S
try:
    lsr = requests.get("https://fapi.binance.com/futures/data/topLongShortPositionRatio",
        params={"symbol": SYM, "period": "15m", "limit": 4}, timeout=5).json()
    print(f"\nTop trader L/S (15m last 4):")
    for r in lsr:
        print(f"  {r['longShortRatio']} L{r['longAccount']}/S{r['shortAccount']}")
except: pass

# Funding history
fund = c.futures_funding_rate(symbol=SYM, limit=5)
print(f"\nFunding history (last 5 × 8h):")
for f in fund:
    print(f"  {f['fundingRate']} ({float(f['fundingRate'])*100*3*365:+.0f}%/yr)")

# Max lev + tick
lev = c._request_futures_api("get", "leverageBracket", True, data={"symbol": SYM})
print(f"\nMax lev: {lev[0]['brackets'][0]['initialLeverage']}x")

info = c.futures_exchange_info()
sym_info = next(s for s in info["symbols"] if s["symbol"]==SYM)
tick = next(f["tickSize"] for f in sym_info["filters"] if f["filterType"]=="PRICE_FILTER")
step = next(f["stepSize"] for f in sym_info["filters"] if f["filterType"]=="LOT_SIZE")
print(f"Tick: {tick}  Step: {step}")

# Risk math
print(f"\n=== SUGGESTED SETUP ===")
entry = mark
sl_pct = -1.5  # tight bounce stop
sl = entry * (1 + sl_pct/100)
tp1_pct = 3.5  # initial TP
tp1 = entry * (1 + tp1_pct/100)
tp2_pct = 7.0  # extended
tp2 = entry * (1 + tp2_pct/100)
print(f"Entry market ~${entry:.5f}")
print(f"SL ${sl:.5f} ({sl_pct}%)")
print(f"TP1 ${tp1:.5f} (+{tp1_pct}%)")
print(f"TP2 ${tp2:.5f} (+{tp2_pct}%)")

for margin in [1.0, 1.5, 2.0]:
    notional = margin * 30
    qty = notional / entry
    sl_loss = qty * (entry - sl)
    tp1_gain = qty * (tp1 - entry)
    tp2_gain = qty * (tp2 - entry)
    print(f"\n  ${margin} margin × 30x = ${notional} notional, qty {qty:.2f}")
    print(f"    Max loss: ${sl_loss:.3f}")
    print(f"    TP1: ${tp1_gain:.3f}  TP2: ${tp2_gain:.3f}")
    print(f"    R:R = {tp1_gain/sl_loss:.2f} (TP1) / {tp2_gain/sl_loss:.2f} (TP2)")
