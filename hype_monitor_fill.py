"""Monitor HYPE breakout order. When fills, auto-place SL/TP.
Also clean up leftover algo SELLs from prior trade."""
# PHASE00_LEGACY_DIRECT_RUN_GUARD
if __name__ == "__main__":
    from legacy_live_blocker import block_file_if_legacy as _phase00_block_file
    _phase00_block_file(__file__, "direct_exec")

from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
from decimal import Decimal
import time
import sys

c = spot_client()
SYM = "HYPEUSDT"
TRIGGER = 58.9
SL_PRICE = 58.30
TP_PRICE = 60.50
QTY = "0.76"

# Cancel leftover reduceOnly SELL algos (from prior HYPE trade)
print("=== Cleaning up leftover algo orders ===")
algos = c._request_futures_api("get", "openAlgoOrders", True, data={"symbol": SYM})
to_cancel = []
keep = None
for a in algos:
    if a["side"] == "SELL" and a.get("reduceOnly"):
        to_cancel.append(a["algoId"])
        print(f"  Will cancel leftover SELL algo {a['algoId']} trigger ${a['triggerPrice']}")
    elif a["side"] == "BUY" and a["triggerPrice"] == "58.9":
        keep = a["algoId"]
        print(f"  Keep BUY breakout algo {a['algoId']} trigger ${a['triggerPrice']}")

for algo_id in to_cancel:
    try:
        c._request_futures_api("delete", "algo/order", True, data={"algoId": algo_id})
        print(f"  Cancelled {algo_id}")
    except Exception as e:
        print(f"  Cancel {algo_id} failed: {e}")

time.sleep(1)
print(f"\n=== Monitoring breakout fill (trigger ${TRIGGER}) ===")
print(f"Will auto-place SL ${SL_PRICE} + TP ${TP_PRICE} on fill")
print("Press Ctrl-C to stop monitor (order stays active)")

filled = False
start = time.time()
last_print = 0
while not filled:
    try:
        pos_list = c.futures_position_information(symbol=SYM)
        pos = next((p for p in pos_list if abs(float(p["positionAmt"]))>0), None)
        if pos:
            entry = float(pos["entryPrice"])
            qty_filled = abs(float(pos["positionAmt"]))
            print(f"\n[FILLED] Entry ${entry}, qty {qty_filled}")
            filled = True
            break
        # Status
        mark = float(c.futures_mark_price(symbol=SYM)["markPrice"])
        if time.time() - last_print > 30:
            print(f"  [{time.strftime('%H:%M:%S')}] mark ${mark:.3f}  distance to trigger: {(TRIGGER-mark)/mark*100:+.2f}%", flush=True)
            last_print = time.time()
        # Timeout 4h
        if time.time() - start > 14400:
            print(f"\n[TIMEOUT 4h] No fill. Cancelling order.")
            c.futures_cancel_all_open_orders(symbol=SYM)
            sys.exit(0)
        time.sleep(10)
    except KeyboardInterrupt:
        print(f"\n[STOPPED] Monitor stopped but breakout order still active.")
        sys.exit(0)
    except Exception as e:
        print(f"  ERR {str(e)[:60]}")
        time.sleep(15)

# After fill: place SL + TP
print(f"\n=== Placing SL + TP ===")
info = c.futures_exchange_info()
sym_info = next(s for s in info["symbols"] if s["symbol"]==SYM)
tick = Decimal(next(f["tickSize"] for f in sym_info["filters"] if f["filterType"]=="PRICE_FILTER"))

sl_q = Decimal(str(SL_PRICE)).quantize(tick)
tp_q = Decimal(str(TP_PRICE)).quantize(tick)

try:
    c.futures_create_order(symbol=SYM, side="SELL", type="STOP_MARKET",
        stopPrice=format(sl_q, "f"), quantity=QTY,
        reduceOnly="true", workingType="MARK_PRICE")
    print(f"[OK] SL placed @ ${sl_q}")
except Exception as e:
    print(f"[FAIL] SL: {e}")

try:
    c.futures_create_order(symbol=SYM, side="SELL", type="TAKE_PROFIT_MARKET",
        stopPrice=format(tp_q, "f"), quantity=QTY,
        reduceOnly="true", workingType="MARK_PRICE")
    print(f"[OK] TP placed @ ${tp_q}")
except Exception as e:
    print(f"[FAIL] TP: {e}")

print(f"\n=== Position protected ===")
print(f"Entry ~${TRIGGER}  SL ${sl_q}  TP ${tp_q}")
