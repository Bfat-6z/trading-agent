# PHASE00_LEGACY_DIRECT_RUN_GUARD
if __name__ == "__main__":
    from legacy_live_blocker import block_file_if_legacy as _phase00_block_file
    _phase00_block_file(__file__, "direct_exec")

from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
import time

c = spot_client()
SYMBOL = "MAGMAUSDT"

# Verify position
pos = c.futures_position_information(symbol=SYMBOL)[0]
qty = abs(float(pos["positionAmt"]))
entry = float(pos["entryPrice"])
mark = float(pos["markPrice"])
print(f"Position: LONG {qty} @ entry=${entry}  mark=${mark}  unPnL=${pos['unRealizedProfit']}")

# Verify no leftover algo orders
orders = c._request_futures_api("get", "openAlgoOrders", True, data={"symbol": SYMBOL})
print(f"Existing algo orders: {len(orders)}")

if qty == 0:
    print("No position. Abort.")
    raise SystemExit(0)

# Place fresh SL + TP
sl_price = round(entry * 0.95, 5)
tp_price = round(entry * 1.08, 5)
print(f"\nPlacing SL @ ${sl_price}")
sl_res = c.futures_create_order(
    symbol=SYMBOL, side="SELL", type="STOP_MARKET",
    stopPrice=str(sl_price), quantity=qty, reduceOnly="true",
)
print(f"  algoId={sl_res.get('algoId')}  status={sl_res.get('algoStatus')}")

time.sleep(1)
print(f"Placing TP @ ${tp_price}")
tp_res = c.futures_create_order(
    symbol=SYMBOL, side="SELL", type="TAKE_PROFIT_MARKET",
    stopPrice=str(tp_price), quantity=qty, reduceOnly="true",
)
print(f"  algoId={tp_res.get('algoId')}  status={tp_res.get('algoStatus')}")

time.sleep(2)
final = c._request_futures_api("get", "openAlgoOrders", True, data={"symbol": SYMBOL})
print(f"\nFinal: {len(final)} algo orders")
for o in final:
    print(f"  {o['orderType']:<22} trigger=${o['triggerPrice']}  qty={o['quantity']}")
