"""Cancel ALL orders on ALGOUSDT then re-place clean SL/TP."""
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
import time

c = spot_client()
SYMBOL = "ALGOUSDT"

# Nuclear cancel
print("Cancelling ALL open orders on ALGOUSDT...")
try:
    res = c.futures_cancel_all_open_orders(symbol=SYMBOL)
    print(f"  Result: {res}")
except Exception as e:
    print(f"  Error: {e}")

time.sleep(2)

# Verify cancelled
remaining = c.futures_get_open_orders(symbol=SYMBOL)
print(f"Remaining orders after cancel: {len(remaining)}")

time.sleep(1)

# Position info
pos = c.futures_position_information(symbol=SYMBOL)[0]
entry = float(pos["entryPrice"])
mark = float(pos["markPrice"])
print(f"\nPosition: entry=${entry} mark=${mark}  unPnL=${pos['unRealizedProfit']}")

# SL at +5% (price up = bad for short)
sl_price = round(entry * 1.05, 4)
# TP at -10% (price down = good for short)
tp_price = round(entry * 0.90, 4)

print(f"SL @ ${sl_price} (entry +5%)")
print(f"TP @ ${tp_price} (entry -10%)")

time.sleep(1)
print("\nPlacing SL...")
try:
    sl = c.futures_create_order(
        symbol=SYMBOL, side="BUY", type="STOP_MARKET",
        stopPrice=str(sl_price), closePosition="true",
    )
    print(f"  SL orderId: {sl.get('orderId')}")
except Exception as e:
    print(f"  SL Error: {e}")

time.sleep(1)
print("Placing TP...")
try:
    tp = c.futures_create_order(
        symbol=SYMBOL, side="BUY", type="TAKE_PROFIT_MARKET",
        stopPrice=str(tp_price), closePosition="true",
    )
    print(f"  TP orderId: {tp.get('orderId')}")
except Exception as e:
    print(f"  TP Error: {e}")

time.sleep(2)
print("\nFinal verification:")
for o in c.futures_get_open_orders(symbol=SYMBOL):
    print(f"  {o.get('type')} side={o.get('side')} stopPrice={o.get('stopPrice')} orderId={o.get('orderId')}")
