from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
c = spot_client()
try:
    r = c.futures_cancel_all_open_orders(symbol="LITUSDT")
    print(f"Cancel result: {r}")
except Exception as e:
    print(f"Err: {e}")
algos = c._request_futures_api("get", "openAlgoOrders", True, data={"symbol":"LITUSDT"})
print(f"Algo orders remaining: {len(algos) if isinstance(algos, list) else '?'}")
for a in algos if isinstance(algos, list) else []:
    print(f"  {a['orderType']} trigger=${a['triggerPrice']}")
