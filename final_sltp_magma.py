"""Place ONE SL + ONE TP on MAGMA LONG."""
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
QTY = 24.0
SL_PRICE = "0.23550"
TP_PRICE = "0.26772"

# Place SL
try:
    sl = c.futures_create_order(
        symbol=SYMBOL, side="SELL", type="STOP_MARKET",
        stopPrice=SL_PRICE, quantity=QTY, reduceOnly="true",
    )
    print(f"SL: algoId={sl.get('algoId')}  orderId={sl.get('orderId')}")
except Exception as e:
    print(f"SL fail: {e}")

time.sleep(1)
try:
    tp = c.futures_create_order(
        symbol=SYMBOL, side="SELL", type="TAKE_PROFIT_MARKET",
        stopPrice=TP_PRICE, quantity=QTY, reduceOnly="true",
    )
    print(f"TP: algoId={tp.get('algoId')}  orderId={tp.get('orderId')}")
except Exception as e:
    print(f"TP fail: {e}")

time.sleep(2)
# Try regular openOrders
print("\nRegular open orders:")
for o in c.futures_get_open_orders(symbol=SYMBOL):
    print(f"  {o.get('type')} side={o.get('side')} stopPrice={o.get('stopPrice')}")

# Try algo via raw call
print("\nAlgo orders (via raw API):")
try:
    res = c._request_futures_api("get", "openAlgoOrders", True, data={"symbol": SYMBOL})
    print(f"  {res}")
except Exception as e:
    print(f"  Endpoint A fail: {e}")
try:
    res = c._request_futures_api("get", "conditionalOrders", True, data={"symbol": SYMBOL})
    print(f"  {res}")
except Exception as e:
    print(f"  Endpoint B fail: {e}")
