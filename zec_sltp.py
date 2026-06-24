from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
from tradingagents.binance import futures as bf
import decimal, time

c = spot_client()
SYMBOL = "ZECUSDT"

filt = bf._futures_filters(SYMBOL)
tick = filt.get("tick_size", 0.01)
step = filt.get("step_size", 0.001)
print(f"Tick: {tick}  Step: {step}")

pos = c.futures_position_information(symbol=SYMBOL)[0]
qty = abs(float(pos["positionAmt"]))
entry = float(pos["entryPrice"])
print(f"Position: SHORT {qty} ZEC @ ${entry}")


def round_tick(price):
    d = decimal.Decimal(str(price))
    t = decimal.Decimal(str(tick))
    return str(float((d // t) * t))


sl_price = round_tick(entry * 1.04)
tp1_price = round_tick(entry * 0.97)
tp2_price = round_tick(entry * 0.94)
half = qty / 2
# Round qty to step
def round_qty(q):
    d = decimal.Decimal(str(q))
    s = decimal.Decimal(str(step))
    return float((d // s) * s)
half = round_qty(half)

print(f"SL @ ${sl_price}  TP1 @ ${tp1_price} (qty {half})  TP2 @ ${tp2_price} (qty {qty-half})")

time.sleep(1)
try:
    sl = c.futures_create_order(symbol=SYMBOL, side="BUY", type="STOP_MARKET",
        stopPrice=sl_price, quantity=qty, reduceOnly="true")
    print(f"SL: algoId={sl.get('algoId')}")
except Exception as e:
    print(f"SL fail: {e}")

time.sleep(1)
try:
    tp1 = c.futures_create_order(symbol=SYMBOL, side="BUY", type="TAKE_PROFIT_MARKET",
        stopPrice=tp1_price, quantity=half, reduceOnly="true")
    print(f"TP1: algoId={tp1.get('algoId')}")
except Exception as e:
    print(f"TP1 fail: {e}")

time.sleep(1)
try:
    tp2 = c.futures_create_order(symbol=SYMBOL, side="BUY", type="TAKE_PROFIT_MARKET",
        stopPrice=tp2_price, quantity=qty - half, reduceOnly="true")
    print(f"TP2: algoId={tp2.get('algoId')}")
except Exception as e:
    print(f"TP2 fail: {e}")

time.sleep(2)
final = c._request_futures_api("get", "openAlgoOrders", True, data={"symbol": SYMBOL})
print(f"\nFinal: {len(final)} orders")
for o in final:
    print(f"  {o['orderType']:<22} trigger=${o['triggerPrice']}  qty={o['quantity']}")
