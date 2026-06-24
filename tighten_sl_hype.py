from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
from decimal import Decimal
c = spot_client()
SYM = "HYPEUSDT"

# Cancel all existing SL/TP
try:
    c.futures_cancel_all_open_orders(symbol=SYM)
    print("All orders cancelled")
except Exception as e:
    print(f"Cancel: {e}")

import time
time.sleep(2)

# Get position
positions = c.futures_position_information(symbol=SYM)
p = next((x for x in positions if abs(float(x["positionAmt"]))>0), None)
if not p:
    print("No position")
else:
    qty = abs(float(p["positionAmt"]))
    entry = float(p["entryPrice"])
    liq = float(p["liquidationPrice"])

    # Max loss target $0.50
    # loss = (entry - sl) * qty = 0.50
    # sl = entry - 0.50/qty
    target_loss = 0.50
    sl_price = entry - (target_loss / qty)

    info = c.futures_exchange_info()
    sym_info = next(s for s in info["symbols"] if s["symbol"]==SYM)
    tick = next(Decimal(f["tickSize"]) for f in sym_info["filters"] if f["filterType"]=="PRICE_FILTER")
    sl_q = Decimal(str(sl_price)).quantize(tick)
    sl_floor = Decimal(str(liq)) * Decimal("1.003")
    final_sl = max(sl_q, sl_floor).quantize(tick)
    tp = Decimal("57.00").quantize(tick)

    print(f"qty: {qty}  entry ${entry}  liq ${liq}")
    print(f"Target SL for -$0.50 loss: ${sl_price:.4f}")
    print(f"After liq floor: ${final_sl}")
    print(f"Actual max loss: ${(float(final_sl)-entry)*qty:+.4f}")
    print(f"TP: ${tp}  Max gain: ${(float(tp)-entry)*qty:+.4f}")

    try:
        c.futures_create_order(symbol=SYM, side="SELL", type="STOP_MARKET",
            stopPrice=format(final_sl, "f"), quantity=qty,
            reduceOnly="true", workingType="CONTRACT_PRICE")
        print(f"\nNEW SL placed @ ${final_sl}")
    except Exception as e:
        print(f"SL fail: {e}")
    try:
        c.futures_create_order(symbol=SYM, side="SELL", type="TAKE_PROFIT_MARKET",
            stopPrice=format(tp, "f"), quantity=qty,
            reduceOnly="true", workingType="CONTRACT_PRICE")
        print(f"TP re-placed @ ${tp}")
    except Exception as e:
        print(f"TP fail: {e}")
