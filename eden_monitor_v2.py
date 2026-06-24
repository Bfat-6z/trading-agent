"""Monitor EDEN breakout fill, auto SL/TP."""
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
from decimal import Decimal
import time, sys

c = spot_client()
SYM = "EDENUSDT"
TRIGGER = 0.0967
SL_PRICE = 0.0950
TP_PRICE = 0.1020
QTY = "207"

print(f"=== Monitor EDEN breakout (trigger ${TRIGGER}) ===", flush=True)

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
            print(f"\n[EDEN FILLED] Entry ${entry}, qty {qty_filled}", flush=True)
            filled = True
            break
        mark = float(c.futures_mark_price(symbol=SYM)["markPrice"])
        if time.time() - last_print > 60:
            dist = (TRIGGER - mark) / mark * 100
            print(f"[{time.strftime('%H:%M:%S')}] EDEN ${mark:.5f}  to trigger: {dist:+.2f}%", flush=True)
            last_print = time.time()
        if time.time() - start > 21600:
            print(f"\n[EDEN TIMEOUT 6h] Cancelling.", flush=True)
            try: c.futures_cancel_all_open_orders(symbol=SYM)
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
info = c.futures_exchange_info()
sym_info = next(s for s in info["symbols"] if s["symbol"]==SYM)
tick = Decimal(next(f["tickSize"] for f in sym_info["filters"] if f["filterType"]=="PRICE_FILTER"))
sl_q = Decimal(str(SL_PRICE)).quantize(tick)
tp_q = Decimal(str(TP_PRICE)).quantize(tick)

try:
    c.futures_create_order(symbol=SYM, side="SELL", type="STOP_MARKET",
        stopPrice=format(sl_q, "f"), quantity=QTY,
        reduceOnly="true", workingType="MARK_PRICE")
    print(f"[OK] EDEN SL @ ${sl_q}", flush=True)
except Exception as e:
    print(f"[FAIL] EDEN SL: {str(e)[:100]}", flush=True)

try:
    c.futures_create_order(symbol=SYM, side="SELL", type="TAKE_PROFIT_MARKET",
        stopPrice=format(tp_q, "f"), quantity=QTY,
        reduceOnly="true", workingType="MARK_PRICE")
    print(f"[OK] EDEN TP @ ${tp_q}", flush=True)
except Exception as e:
    print(f"[FAIL] EDEN TP: {str(e)[:100]}", flush=True)

print(f"\n=== EDEN position protected ===\nEntry ~${TRIGGER}  SL ${sl_q}  TP ${tp_q}", flush=True)
