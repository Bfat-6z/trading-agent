"""Place SL/TP on MAGMA LONG position. Multiple param strategies."""
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
import time

c = spot_client()
SYMBOL = "MAGMAUSDT"

pos = c.futures_position_information(symbol=SYMBOL)[0]
qty = abs(float(pos["positionAmt"]))
entry = float(pos["entryPrice"])
print(f"Position: LONG {qty} @ ${entry}")

sl_price = round(entry * 0.95, 5)
tp_price = round(entry * 1.08, 5)
print(f"SL @ ${sl_price}  TP @ ${tp_price}")

# Cancel any existing
try:
    c.futures_cancel_all_open_orders(symbol=SYMBOL)
    print("Cancelled all existing orders")
except Exception:
    pass
time.sleep(1)

# Try various parameter combos to find working
attempts = [
    {"name": "STOP_MARKET reduceOnly=true", "params": {
        "symbol": SYMBOL, "side": "SELL", "type": "STOP_MARKET",
        "stopPrice": str(sl_price), "quantity": qty, "reduceOnly": "true",
    }},
    {"name": "STOP_MARKET reduceOnly=True bool", "params": {
        "symbol": SYMBOL, "side": "SELL", "type": "STOP_MARKET",
        "stopPrice": str(sl_price), "quantity": qty, "reduceOnly": True,
    }},
    {"name": "STOP_MARKET no reduce, just qty", "params": {
        "symbol": SYMBOL, "side": "SELL", "type": "STOP_MARKET",
        "stopPrice": str(sl_price), "quantity": qty,
    }},
]

for a in attempts:
    print(f"\nTrying: {a['name']}")
    try:
        res = c.futures_create_order(**a["params"])
        print(f"  Response: {res}")
        if res.get("orderId"):
            print(f"  ✓ SUCCESS orderId={res.get('orderId')}")
            break
    except Exception as e:
        print(f"  Error: {e}")

# Same for TP
print("\n=== TP ===")
tp_attempts = [
    {"name": "TAKE_PROFIT_MARKET reduceOnly=true", "params": {
        "symbol": SYMBOL, "side": "SELL", "type": "TAKE_PROFIT_MARKET",
        "stopPrice": str(tp_price), "quantity": qty, "reduceOnly": "true",
    }},
    {"name": "TAKE_PROFIT_MARKET reduceOnly=True bool", "params": {
        "symbol": SYMBOL, "side": "SELL", "type": "TAKE_PROFIT_MARKET",
        "stopPrice": str(tp_price), "quantity": qty, "reduceOnly": True,
    }},
    {"name": "TAKE_PROFIT_MARKET no reduce", "params": {
        "symbol": SYMBOL, "side": "SELL", "type": "TAKE_PROFIT_MARKET",
        "stopPrice": str(tp_price), "quantity": qty,
    }},
]
for a in tp_attempts:
    print(f"\nTrying: {a['name']}")
    try:
        res = c.futures_create_order(**a["params"])
        print(f"  Response: {res}")
        if res.get("orderId"):
            print(f"  ✓ SUCCESS orderId={res.get('orderId')}")
            break
    except Exception as e:
        print(f"  Error: {e}")

time.sleep(2)
print("\nFinal orders:")
for o in c.futures_get_open_orders(symbol=SYMBOL):
    print(f"  {o.get('type')} side={o.get('side')} stopPrice={o.get('stopPrice')} orderId={o.get('orderId')}")
