# PHASE00_LEGACY_DIRECT_RUN_GUARD
if __name__ == "__main__":
    from legacy_live_blocker import block_file_if_legacy as _phase00_block_file
    _phase00_block_file(__file__, "direct_exec")

from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
import time

c = spot_client()
SYMBOL = "ALGOUSDT"
QTY = 50.4  # absolute qty to close

pos = c.futures_position_information(symbol=SYMBOL)[0]
entry = float(pos["entryPrice"])
mark = float(pos["markPrice"])
print(f"Position: entry=${entry} mark=${mark}  unPnL=${pos['unRealizedProfit']}")

sl_price = round(entry * 1.05, 4)
tp_price = round(entry * 0.90, 4)
print(f"SL @ ${sl_price}, TP @ ${tp_price}")

# Reduce-only SL
print("\nSL with reduceOnly=true...")
try:
    sl = c.futures_create_order(
        symbol=SYMBOL, side="BUY", type="STOP_MARKET",
        stopPrice=str(sl_price), quantity=QTY, reduceOnly="true",
    )
    print(f"  SL orderId: {sl.get('orderId')}")
except Exception as e:
    print(f"  SL Error: {e}")

time.sleep(1)
print("TP with reduceOnly=true...")
try:
    tp = c.futures_create_order(
        symbol=SYMBOL, side="BUY", type="TAKE_PROFIT_MARKET",
        stopPrice=str(tp_price), quantity=QTY, reduceOnly="true",
    )
    print(f"  TP orderId: {tp.get('orderId')}")
except Exception as e:
    print(f"  TP Error: {e}")

time.sleep(2)
print("\nFinal:")
for o in c.futures_get_open_orders(symbol=SYMBOL):
    print(f"  {o.get('type')} side={o.get('side')} stopPrice={o.get('stopPrice')} qty={o.get('origQty')} reduceOnly={o.get('reduceOnly')}")
