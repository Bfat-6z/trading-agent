from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
from decimal import Decimal
import time
c = spot_client()
SYM = "HYPEUSDT"

try:
    c.futures_cancel_all_open_orders(symbol=SYM)
    print("Cancelled")
except Exception as e:
    print(f"Cancel: {e}")
time.sleep(2)

positions = c.futures_position_information(symbol=SYM)
p = next((x for x in positions if abs(float(x["positionAmt"]))>0), None)
if not p:
    print("No position")
else:
    qty = abs(float(p["positionAmt"]))
    entry = float(p["entryPrice"])
    info = c.futures_exchange_info()
    sym_info = next(s for s in info["symbols"] if s["symbol"]==SYM)
    tick = next(Decimal(f["tickSize"]) for f in sym_info["filters"] if f["filterType"]=="PRICE_FILTER")
    # Trail SL to lock min +$1 profit
    target_lock_profit = 1.00
    sl_price = entry + (target_lock_profit / qty) + entry * 0.0006  # +fees buffer
    sl = Decimal(str(sl_price)).quantize(tick)
    tp = Decimal("57.00").quantize(tick)
    print(f"Entry ${entry}  qty {qty}")
    print(f"NEW SL @ BE ${sl}  TP ${tp}")
    print(f"Worst case: ~$0 (BE)  Best: +${(float(tp)-entry)*qty:.4f}")
    try:
        c.futures_create_order(symbol=SYM, side="SELL", type="STOP_MARKET",
            stopPrice=format(sl, "f"), quantity=qty,
            reduceOnly="true", workingType="CONTRACT_PRICE")
        print("BE SL placed")
    except Exception as e:
        print(f"SL fail: {e}")
    try:
        c.futures_create_order(symbol=SYM, side="SELL", type="TAKE_PROFIT_MARKET",
            stopPrice=format(tp, "f"), quantity=qty,
            reduceOnly="true", workingType="CONTRACT_PRICE")
        print("TP re-placed")
    except Exception as e:
        print(f"TP fail: {e}")
