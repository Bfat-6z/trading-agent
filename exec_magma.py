"""Operator override: LONG MAGMA oversold bounce."""
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance import futures as bf
from tradingagents.binance.client import spot_client
import time

SYMBOL = "MAGMAUSDT"
MARGIN = 2.0
LEVERAGE = 3
SL_PCT = 5.0
TP_PCT = 8.0
DIRECTION = "LONG"

c = spot_client()
print(f"OPERATOR override: LONG {SYMBOL}  margin=${MARGIN}  lev={LEVERAGE}x")

bal = bf.get_futures_balance()
print(f"Avail: ${bal['available']:.4f}")
if bal['available'] < MARGIN + 0.1:
    print("Insufficient balance")
    raise SystemExit(1)

mark = float(c.futures_mark_price(symbol=SYMBOL)["markPrice"])
print(f"Mark: ${mark}")

res = bf.open_long(SYMBOL, MARGIN, leverage=LEVERAGE, isolated=True)
print(f"\nORDER: id={res.order_id}  qty={res.executed_qty}  avgPrice=${res.avg_price}")

entry = res.avg_price if res.avg_price > 0 else mark
sl = entry * (1 - SL_PCT / 100)
tp = entry * (1 + TP_PCT / 100)
print(f"SL @ ${sl:.6f}  TP @ ${tp:.6f}")

time.sleep(1)
try:
    sl_res = bf.place_stop_loss(SYMBOL, sl, side_to_close="LONG")
    print(f"  SL orderId: {sl_res.get('orderId')}")
except Exception as e:
    print(f"  SL failed: {e}")

time.sleep(1)
try:
    tp_res = bf.place_take_profit(SYMBOL, tp, side_to_close="LONG")
    print(f"  TP orderId: {tp_res.get('orderId')}")
except Exception as e:
    print(f"  TP failed: {e}")

time.sleep(1)
pos = bf.get_position(SYMBOL)
print(f"\nPosition: {pos}")
print("\nOpen orders:")
for o in c.futures_get_open_orders(symbol=SYMBOL):
    print(f"  {o.get('type')} side={o.get('side')} stopPrice={o.get('stopPrice')}")
