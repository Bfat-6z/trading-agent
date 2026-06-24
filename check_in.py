"""Check IN setup live - waiting for retest of $0.0920 to short."""
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
import requests
c = spot_client()
SYM = "INUSDT"

t = c.futures_ticker(symbol=SYM)
mark = float(c.futures_mark_price(symbol=SYM)["markPrice"])
klines_5m = c.futures_klines(symbol=SYM, interval="5m", limit=12)
klines_15m = c.futures_klines(symbol=SYM, interval="15m", limit=20)

print(f"=== {SYM} ===")
print(f"Mark: ${mark:.5f}  ch24: {t['priceChangePercent']}%")
print(f"24h hi: ${float(t['highPrice']):.5f}  lo: ${float(t['lowPrice']):.5f}")
print(f"Vol: ${float(t['quoteVolume'])/1e6:.1f}M")

print(f"\nLast 6 5m candles:")
for k in klines_5m[-6:]:
    o = float(k[1]); h = float(k[2]); l = float(k[3]); cl = float(k[4])
    print(f"  O:{o:.5f} H:{h:.5f} L:{l:.5f} C:{cl:.5f}")

print(f"\nLast 4 15m candles:")
for k in klines_15m[-4:]:
    o = float(k[1]); h = float(k[2]); l = float(k[3]); cl = float(k[4])
    print(f"  O:{o:.5f} H:{h:.5f} L:{l:.5f} C:{cl:.5f}")

try:
    flow = requests.get("https://fapi.binance.com/futures/data/takerlongshortRatio",
        params={"symbol": SYM, "period": "5m", "limit": 6}, timeout=10).json()
    print(f"\n5m taker flow last 6:")
    for f in flow:
        print(f"  {f['buySellRatio']}")
except Exception as e:
    print(f"flow err: {e}")

try:
    oi = requests.get("https://fapi.binance.com/futures/data/openInterestHist",
        params={"symbol": SYM, "period": "15m", "limit": 8}, timeout=10).json()
    print(f"\nOI last 8 × 15m:")
    for o in oi:
        print(f"  {float(o['sumOpenInterest']):,.0f}")
except Exception as e:
    print(f"oi err: {e}")

trigger = 0.0920
target_entry_low = 0.0905
target_entry_high = 0.0910
print(f"\n=== Entry Trigger ===")
print(f"Target entry: ${target_entry_low}-${target_entry_high}")
print(f"Current ${mark:.5f}: {'AT TRIGGER' if target_entry_low <= mark <= target_entry_high else 'WAIT'}")
print(f"  Distance: {(mark - target_entry_high)*100/mark:+.2f}%")
print(f"SL: $0.0958")
print(f"TP1: $0.0855 (-5%)  TP2: $0.0820 (-9%)")
