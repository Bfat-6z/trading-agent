# PHASE00_LEGACY_DIRECT_RUN_GUARD
if __name__ == "__main__":
    from legacy_live_blocker import block_file_if_legacy as _phase00_block_file
    _phase00_block_file(__file__, "direct_exec")

from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
c = spot_client()
SYM = "BSBUSDT"

# Get current state
t = c.futures_ticker(symbol=SYM)
mark = float(c.futures_mark_price(symbol=SYM)["markPrice"])
funding = c.futures_funding_rate(symbol=SYM, limit=3)
oi = c.futures_open_interest(symbol=SYM)
import requests
try:
    oi_hist = requests.get("https://fapi.binance.com/futures/data/openInterestHist", params={"symbol": SYM, "period": "1h", "limit": 24}, timeout=10).json()
except Exception as e:
    print(f"OI hist fail: {e}")
    oi_hist = []
klines_15m = c.futures_klines(symbol=SYM, interval="15m", limit=20)
klines_5m = c.futures_klines(symbol=SYM, interval="5m", limit=20)

print(f"=== BSB live state ===")
print(f"Mark: ${mark:.4f}")
print(f"24h change: {t['priceChangePercent']}%")
print(f"24h high: ${float(t['highPrice']):.4f}")
print(f"24h low: ${float(t['lowPrice']):.4f}")
print(f"24h vol: ${float(t['quoteVolume'])/1e6:.1f}M")
print(f"\nFunding rate (latest 3):")
for f in funding[-3:]:
    print(f"  {f['fundingRate']} ({float(f['fundingRate'])*100*365*3:.1f}% annual)")
print(f"\nOI: ${float(oi['openInterest'])*mark/1e6:.1f}M USD")

# OI 24h
oi_24h_ago = float(oi_hist[0]["sumOpenInterest"]) if oi_hist else 0
oi_now = float(oi_hist[-1]["sumOpenInterest"]) if oi_hist else 0
print(f"OI 24h change: {(oi_now/oi_24h_ago - 1)*100:.1f}%")

# Last 4 candles 15m
print(f"\nLast 4 15m candles:")
for k in klines_15m[-4:]:
    o = float(k[1]); h = float(k[2]); l = float(k[3]); cl = float(k[4]); v = float(k[5])
    bar_pct = (cl - o) / o * 100
    print(f"  O:{o:.4f} H:{h:.4f} L:{l:.4f} C:{cl:.4f} ({bar_pct:+.2f}%) vol:{v:.0f}")

# VWAP rough
total_v = sum(float(k[5]) for k in klines_15m)
total_pv = sum((float(k[2])+float(k[3])+float(k[4]))/3 * float(k[5]) for k in klines_15m)
vwap = total_pv / total_v if total_v > 0 else mark
print(f"\nVWAP (last 20×15m): ${vwap:.4f}  Mark vs VWAP: {(mark/vwap-1)*100:+.2f}%")

# Range position
hi24 = float(t['highPrice']); lo24 = float(t['lowPrice'])
rng_pos = (mark - lo24) / (hi24 - lo24) if hi24 > lo24 else 0.5
print(f"Range position 24h: {rng_pos*100:.0f}%")

# Last 4 5m for momentum
print(f"\nLast 4 5m candles:")
for k in klines_5m[-4:]:
    o = float(k[1]); h = float(k[2]); l = float(k[3]); cl = float(k[4])
    bar_pct = (cl - o) / o * 100
    print(f"  O:{o:.4f} H:{h:.4f} L:{l:.4f} C:{cl:.4f} ({bar_pct:+.2f}%)")

# SHORT signals checklist
print(f"\n=== SHORT SIGNALS CHECKLIST ===")
sig1 = "PASS" if rng_pos < 0.9 and mark < hi24 * 0.99 else "FAIL"
print(f"1. Lower-high vs 24h high (<99% of hi)? {sig1} (mark/hi24={mark/hi24:.2%})")
last_15m_c = float(klines_15m[-1][4]); last_15m_o = float(klines_15m[-1][1])
sig2 = "PASS" if last_15m_c < last_15m_o else "FAIL"
print(f"2. Latest 15m bearish? {sig2}")
sig3 = "WAIT" if float(funding[-1]["fundingRate"]) < 0.001 else "PASS"
print(f"3. Funding tick >+0.1%/8h? {sig3} (current: {float(funding[-1]['fundingRate'])*100:.3f}%)")
sig4 = "PASS" if mark < vwap else "FAIL"
print(f"4. 5m below VWAP? {sig4}")

print(f"\n=== Max leverage check ===")
try:
    lev = c._request_futures_api("get", "leverageBracket", True, data={"symbol": SYM})
    max_lev = lev[0]["brackets"][0]["initialLeverage"]
    print(f"Max leverage: {max_lev}x")
except Exception as e:
    print(f"Lev check: {e}")
