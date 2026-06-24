"""Manual operator override: open ALGO short."""
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance import futures as bf
from tradingagents.binance.client import spot_client
import sys, time

SYMBOL = sys.argv[1] if len(sys.argv) > 1 else "ALGOUSDT"
MARGIN = float(sys.argv[2]) if len(sys.argv) > 2 else 1.5
LEVERAGE = int(sys.argv[3]) if len(sys.argv) > 3 else 3
DIRECTION = sys.argv[4].upper() if len(sys.argv) > 4 else "SHORT"
SL_PCT = 5.0
TP_PCT = 10.0

c = spot_client()
print(f"OPERATOR override: {DIRECTION} {SYMBOL}  margin=${MARGIN}  leverage={LEVERAGE}x")

bal = bf.get_futures_balance()
print(f"Available: ${bal['available']:.4f}")
if bal['available'] < MARGIN + 0.05:
    print("Insufficient balance")
    sys.exit(1)

# Mark price
mark = float(c.futures_mark_price(symbol=SYMBOL)["markPrice"])
print(f"Mark price: ${mark}")

if DIRECTION == "SHORT":
    res = bf.open_short(SYMBOL, MARGIN, leverage=LEVERAGE, isolated=True)
    sl_price = mark * (1 + SL_PCT / 100)
    tp_price = mark * (1 - TP_PCT / 100)
    side_to_close = "SHORT"
else:
    res = bf.open_long(SYMBOL, MARGIN, leverage=LEVERAGE, isolated=True)
    sl_price = mark * (1 - SL_PCT / 100)
    tp_price = mark * (1 + TP_PCT / 100)
    side_to_close = "LONG"

print(f"\nORDER EXECUTED:")
print(f"  Order ID: {res.order_id}")
print(f"  Filled qty: {res.executed_qty}")
print(f"  Avg price: ${res.avg_price}")

# Set SL + TP
print(f"\nSetting SL @ ${sl_price:.6f} (target -{SL_PCT}%)")
try:
    sl_res = bf.place_stop_loss(SYMBOL, sl_price, side_to_close=side_to_close)
    print(f"  SL order ID: {sl_res.get('orderId')}")
except Exception as e:
    print(f"  SL setup failed: {e}")

print(f"\nSetting TP @ ${tp_price:.6f} (target +{TP_PCT}%)")
try:
    tp_res = bf.place_take_profit(SYMBOL, tp_price, side_to_close=side_to_close)
    print(f"  TP order ID: {tp_res.get('orderId')}")
except Exception as e:
    print(f"  TP setup failed: {e}")

# Verify
time.sleep(1)
pos = bf.get_position(SYMBOL)
print(f"\nPosition verified:")
for k, v in pos.items():
    print(f"  {k}: {v}")
