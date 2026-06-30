"""Monitor HYPE breakout fill, then place SL/TP. Skip cleanup (API 404)."""
# PHASE00_LEGACY_DIRECT_RUN_GUARD
if __name__ == "__main__":
    from legacy_live_blocker import block_file_if_legacy as _phase00_block_file
    _phase00_block_file(__file__, "direct_exec")

from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
from decimal import Decimal
import time, sys

c = spot_client()
SYM = "HYPEUSDT"
TRIGGER = 58.9
SL_PRICE = 58.30
TP_PRICE = 60.50
QTY = "0.76"

print(f"=== Monitor HYPE breakout (trigger ${TRIGGER}) ===")
print(f"Will auto SL ${SL_PRICE} + TP ${TP_PRICE} on fill. 4h timeout.")
sys.stdout.flush()

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
            print(f"\n[FILLED] Entry ${entry}, qty {qty_filled}", flush=True)
            filled = True
            break
        mark = float(c.futures_mark_price(symbol=SYM)["markPrice"])
        if time.time() - last_print > 60:
            dist = (TRIGGER - mark) / mark * 100
            print(f"[{time.strftime('%H:%M:%S')}] mark ${mark:.3f}  to trigger: {dist:+.2f}%", flush=True)
            last_print = time.time()
        if time.time() - start > 14400:
            print(f"\n[TIMEOUT 4h] No fill. Cancelling breakout order.", flush=True)
            try:
                c.futures_cancel_all_open_orders(symbol=SYM)
            except: pass
            sys.exit(0)
        time.sleep(15)
    except KeyboardInterrupt:
        print(f"\n[STOPPED]", flush=True)
        sys.exit(0)
    except Exception as e:
        print(f"  ERR {str(e)[:60]}", flush=True)
        time.sleep(20)

# Place SL + TP
print(f"\n=== Placing SL + TP ===", flush=True)
info = c.futures_exchange_info()
sym_info = next(s for s in info["symbols"] if s["symbol"]==SYM)
tick = Decimal(next(f["tickSize"] for f in sym_info["filters"] if f["filterType"]=="PRICE_FILTER"))
sl_q = Decimal(str(SL_PRICE)).quantize(tick)
tp_q = Decimal(str(TP_PRICE)).quantize(tick)

try:
    c.futures_create_order(symbol=SYM, side="SELL", type="STOP_MARKET",
        stopPrice=format(sl_q, "f"), quantity=QTY,
        reduceOnly="true", workingType="MARK_PRICE")
    print(f"[OK] SL @ ${sl_q}", flush=True)
except Exception as e:
    print(f"[FAIL] SL: {str(e)[:100]}", flush=True)

try:
    c.futures_create_order(symbol=SYM, side="SELL", type="TAKE_PROFIT_MARKET",
        stopPrice=format(tp_q, "f"), quantity=QTY,
        reduceOnly="true", workingType="MARK_PRICE")
    print(f"[OK] TP @ ${tp_q}", flush=True)
except Exception as e:
    print(f"[FAIL] TP: {str(e)[:100]}", flush=True)

print(f"\n=== Position protected ===\nEntry ~${TRIGGER}  SL ${sl_q}  TP ${tp_q}", flush=True)
