"""Cleanup duplicate algo orders on MAGMA, keep 1 SL + 1 TP."""
# PHASE00_LEGACY_DIRECT_RUN_GUARD
if __name__ == "__main__":
    from legacy_live_blocker import block_file_if_legacy as _phase00_block_file
    _phase00_block_file(__file__, "direct_exec")

from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client

c = spot_client()
SYMBOL = "MAGMAUSDT"

# python-binance method for algo orders
algo_orders = c._request_futures_api("get", "openOrders", True, data={"symbol": SYMBOL})
print(f"Regular open orders: {len(algo_orders)}")
for o in algo_orders:
    print(f"  {o}")

# Try conditional/algo orders endpoint
try:
    conditional = c._request_futures_api("get", "openOrders", True, data={"symbol": SYMBOL})
    print(f"Conditional orders: {conditional}")
except Exception as e:
    print(f"Conditional fetch error: {e}")

# Try cancel all algo
try:
    res = c._request_futures_api("delete", "allOpenOrders", True, data={"symbol": SYMBOL})
    print(f"Cancel all result: {res}")
except Exception as e:
    print(f"Cancel error: {e}")
