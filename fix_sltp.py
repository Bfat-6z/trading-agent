"""Set SL/TP on existing position with proper tick size rounding."""
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance import futures as bf
from tradingagents.binance.client import spot_client
import decimal

SYMBOL = "ALGOUSDT"
SL_PCT = 5.0
TP_PCT = 10.0
SIDE_TO_CLOSE = "SHORT"

c = spot_client()
pos = bf.get_position(SYMBOL)
print(f"Position: {pos}")
entry = pos["entry_price"]

# Get tick size
filt = bf._futures_filters(SYMBOL)
tick = filt.get("tick_size", 0.0001)
print(f"Tick size: {tick}")


def round_to_tick(price, tick):
    """Round price to nearest tick multiple."""
    d = decimal.Decimal(str(price))
    t = decimal.Decimal(str(tick))
    return float((d // t) * t)


if SIDE_TO_CLOSE == "SHORT":
    sl_raw = entry * (1 + SL_PCT / 100)
    tp_raw = entry * (1 - TP_PCT / 100)
else:
    sl_raw = entry * (1 - SL_PCT / 100)
    tp_raw = entry * (1 + TP_PCT / 100)

# Round to tick
sl_price = round_to_tick(sl_raw, tick)
tp_price = round_to_tick(tp_raw, tick)
print(f"SL raw {sl_raw} -> rounded {sl_price}")
print(f"TP raw {tp_raw} -> rounded {tp_price}")

# Cancel any existing orders first
try:
    open_orders = c.futures_get_open_orders(symbol=SYMBOL)
    for o in open_orders:
        c.futures_cancel_order(symbol=SYMBOL, orderId=o["orderId"])
        print(f"Cancelled old order {o['orderId']}")
except Exception as e:
    print(f"Cancel error: {e}")

# Place SL
close_side = "BUY" if SIDE_TO_CLOSE == "SHORT" else "SELL"
try:
    sl_res = c.futures_create_order(
        symbol=SYMBOL, side=close_side, type="STOP_MARKET",
        stopPrice=str(sl_price), closePosition=True,
    )
    print(f"SL placed: orderId={sl_res.get('orderId')}")
except Exception as e:
    print(f"SL failed: {e}")

# Place TP
try:
    tp_res = c.futures_create_order(
        symbol=SYMBOL, side=close_side, type="TAKE_PROFIT_MARKET",
        stopPrice=str(tp_price), closePosition=True,
    )
    print(f"TP placed: orderId={tp_res.get('orderId')}")
except Exception as e:
    print(f"TP failed: {e}")

# Verify
import time; time.sleep(1)
final = c.futures_get_open_orders(symbol=SYMBOL)
print(f"\nFinal open orders on {SYMBOL}:")
for o in final:
    print(f"  {o['type']} {o['side']} stopPrice={o.get('stopPrice')} status={o['status']}")
