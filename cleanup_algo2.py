"""Cancel duplicate algo orders, keep most recent SL + TP."""
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
import time

c = spot_client()
SYMBOL = "MAGMAUSDT"

orders = c._request_futures_api("get", "openAlgoOrders", True, data={"symbol": SYMBOL})
print(f"Total algo orders: {len(orders)}")

# Sort by createTime descending — newest first
orders.sort(key=lambda o: o["createTime"], reverse=True)

# Keep newest 1 SL and 1 TP, cancel others
kept_sl = False
kept_tp = False
to_cancel = []
keepers = []
for o in orders:
    typ = o["orderType"]
    if typ == "STOP_MARKET" and not kept_sl:
        kept_sl = True
        keepers.append(o)
    elif typ == "TAKE_PROFIT_MARKET" and not kept_tp:
        kept_tp = True
        keepers.append(o)
    else:
        to_cancel.append(o)

print(f"\nKeeping ({len(keepers)}):")
for o in keepers:
    print(f"  {o['orderType']} algoId={o['algoId']} trigger=${o['triggerPrice']}")

print(f"\nCancelling ({len(to_cancel)}):")
for o in to_cancel:
    try:
        # Try different cancel endpoints
        res = c._request_futures_api(
            "delete", "order", True,
            data={"symbol": SYMBOL, "orderId": o["algoId"]},
        )
        print(f"  Cancelled algoId={o['algoId']}: {res}")
    except Exception as e:
        # Try as algo cancel
        try:
            res = c._request_futures_api(
                "delete", "algo/order", True,
                data={"symbol": SYMBOL, "algoId": o["algoId"]},
            )
            print(f"  Cancelled (algo) algoId={o['algoId']}: {res}")
        except Exception as e2:
            print(f"  Failed algoId={o['algoId']}: {e2}")

time.sleep(2)
final = c._request_futures_api("get", "openAlgoOrders", True, data={"symbol": SYMBOL})
print(f"\nFinal algo orders: {len(final)}")
for o in final:
    print(f"  {o['orderType']} algoId={o['algoId']} trigger=${o['triggerPrice']}")
