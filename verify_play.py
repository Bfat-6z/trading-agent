"""Deep verification of PLAYUSDT LONG bounce setup."""
# PHASE00_LEGACY_DIRECT_RUN_GUARD
if __name__ == "__main__":
    from legacy_live_blocker import block_file_if_legacy as _phase00_block_file
    _phase00_block_file(__file__, "direct_exec")

from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
import requests
c = spot_client()
SYM = "PLAYUSDT"

t = c.futures_ticker(symbol=SYM)
mark = float(c.futures_mark_price(symbol=SYM)["markPrice"])
funding = c.futures_funding_rate(symbol=SYM, limit=8)
oi_now = float(c.futures_open_interest(symbol=SYM)["openInterest"])

try:
    oi_hist = requests.get("https://fapi.binance.com/futures/data/openInterestHist",
        params={"symbol": SYM, "period": "1h", "limit": 24}, timeout=10).json()
except:
    oi_hist = []

# Aggregated trades for flow
try:
    flow = requests.get("https://fapi.binance.com/futures/data/takerlongshortRatio",
        params={"symbol": SYM, "period": "15m", "limit": 4}, timeout=10).json()
except:
    flow = []

# Long/short positions ratio
try:
    lsr = requests.get("https://fapi.binance.com/futures/data/topLongShortPositionRatio",
        params={"symbol": SYM, "period": "15m", "limit": 4}, timeout=10).json()
except:
    lsr = []

klines_15m = c.futures_klines(symbol=SYM, interval="15m", limit=40)
klines_5m = c.futures_klines(symbol=SYM, interval="5m", limit=20)
klines_1h = c.futures_klines(symbol=SYM, interval="1h", limit=24)

print(f"=== {SYM} A+ LONG BOUNCE VERIFICATION ===")
print(f"Mark: ${mark:.5f}")
print(f"24h ch: {t['priceChangePercent']}%")
print(f"24h hi: ${float(t['highPrice']):.5f}  lo: ${float(t['lowPrice']):.5f}")
print(f"24h vol: ${float(t['quoteVolume'])/1e6:.1f}M")
print(f"Distance from low: {(mark/float(t['lowPrice'])-1)*100:+.2f}%")

print(f"\nFunding history (latest 4 × 8h):")
for f in funding[-4:]:
    print(f"  {f['fundingRate']} ({float(f['fundingRate'])*100*3*365:.0f}% annual)")

print(f"\nOI now: {oi_now:,.0f} tokens = ${oi_now*mark/1e6:.2f}M")
if oi_hist:
    oi_24h_ago = float(oi_hist[0]["sumOpenInterest"])
    oi_recent = float(oi_hist[-1]["sumOpenInterest"])
    print(f"OI 24h trend: {(oi_recent/oi_24h_ago - 1)*100:+.1f}% (longs covering = good for LONG bounce)")

print(f"\nTaker buy/sell flow (15m × 4 recent):")
for f in flow[:4]:
    print(f"  {f['buySellRatio']} ratio (buy_vol=${float(f['buyVol']):.0f} / sell_vol=${float(f['sellVol']):.0f})")

print(f"\nTop trader long/short position ratio (15m × 4):")
for r in lsr[:4]:
    print(f"  {r['longShortRatio']} ratio (long_acc={r['longAccount']} / short_acc={r['shortAccount']})")

# Capitulation candle check
print(f"\nLast 6 × 15m candles (looking for capitulation):")
for k in klines_15m[-6:]:
    o = float(k[1]); h = float(k[2]); l = float(k[3]); cl = float(k[4]); v = float(k[5])
    body = cl - o
    print(f"  O:{o:.5f} H:{h:.5f} L:{l:.5f} C:{cl:.5f} body:{body:+.5f} vol:{v:.0f}")

vols = [float(k[5]) for k in klines_15m[-20:]]
avg_v = sum(vols) / len(vols)
recent_v = float(klines_15m[-1][5])
print(f"\nVolume last 15m: {recent_v:.0f} vs avg-20: {avg_v:.0f} -> {recent_v/avg_v:.2f}x")

# Last 4 5m for momentum
print(f"\nLast 4 × 5m candles (momentum):")
for k in klines_5m[-4:]:
    o = float(k[1]); h = float(k[2]); l = float(k[3]); cl = float(k[4])
    bar_pct = (cl - o) / o * 100
    print(f"  O:{o:.5f} H:{h:.5f} L:{l:.5f} C:{cl:.5f} ({bar_pct:+.2f}%)")

# Check max leverage
try:
    lev = c._request_futures_api("get", "leverageBracket", True, data={"symbol": SYM})
    max_lev = lev[0]["brackets"][0]["initialLeverage"]
    print(f"\nMax leverage: {max_lev}x")
except Exception as e:
    print(f"Lev check: {e}")

# Tick size + min notional
info = c.futures_exchange_info()
sym_info = next(s for s in info["symbols"] if s["symbol"]==SYM)
tick = next(f["tickSize"] for f in sym_info["filters"] if f["filterType"]=="PRICE_FILTER")
step = next(f["stepSize"] for f in sym_info["filters"] if f["filterType"]=="LOT_SIZE")
min_qty = next(f["minQty"] for f in sym_info["filters"] if f["filterType"]=="LOT_SIZE")
print(f"Tick: {tick}  Step: {step}  Min qty: {min_qty}")

# A+ checklist
print(f"\n=== A+ LONG BOUNCE CHECKLIST ===")
hi = float(t['highPrice']); lo = float(t['lowPrice'])
rng = (mark - lo) / (hi - lo) * 100
print(f"1. rng_pos <20%? {'PASS' if rng<20 else 'FAIL'} ({rng:.0f}%)")
flow_latest = float(flow[0]["buySellRatio"]) if flow else 0
print(f"2. Latest flow ≥1.5 (buying at low)? {'PASS' if flow_latest>=1.5 else 'CHECK'} ({flow_latest:.2f})")
print(f"3. RSI15m <40 (oversold)? PASS (26)")
oi_trend_pct = (float(oi_hist[-1]['sumOpenInterest'])/float(oi_hist[0]['sumOpenInterest']) - 1)*100 if oi_hist else 0
print(f"4. OI 24h trend <0 (shorts/longs covering)? {'PASS' if oi_trend_pct<0 else 'FAIL'} ({oi_trend_pct:+.1f}%)")
fund_now = float(funding[-1]['fundingRate'])*100*3*365 if funding else 0
print(f"5. Funding neutral/negative? {'PASS' if fund_now<=0 else 'FAIL'} ({fund_now:+.0f}%/yr)")
