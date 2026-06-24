"""EDEN reclaim watch. Alert when $0.0967 reclaimed + flow >1.5 sustained.
Does NOT place order automatically - just alerts."""
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
import requests, time, sys

c = spot_client()
SYM = "EDENUSDT"
RECLAIM_LEVEL = 0.0967
SWEEP_LOW_LEVEL = 0.0940  # if sweep this + reclaim = also good

print(f"=== EDEN Reclaim Watch ===", flush=True)
print(f"Alert when: mark > ${RECLAIM_LEVEL} AND flow >= 1.5 for 2 consecutive 5m candles", flush=True)
print(f"OR: mark sweeps ${SWEEP_LOW_LEVEL} then closes back above\n", flush=True)

last_print = 0
flow_history = []
start = time.time()

while True:
    try:
        mark = float(c.futures_mark_price(symbol=SYM)["markPrice"])
        try:
            flow_data = requests.get("https://fapi.binance.com/futures/data/takerlongshortRatio",
                params={"symbol": SYM, "period": "5m", "limit": 2}, timeout=5).json()
            flow_now = float(flow_data[-1]["buySellRatio"])
            flow_prev = float(flow_data[-2]["buySellRatio"])
        except:
            flow_now = flow_prev = 0

        # Status every 60s
        if time.time() - last_print > 60:
            print(f"[{time.strftime('%H:%M:%S')}] mark ${mark:.5f}  to reclaim: {(RECLAIM_LEVEL/mark-1)*100:+.2f}%  flow: {flow_prev:.2f}/{flow_now:.2f}", flush=True)
            last_print = time.time()

        # Reclaim signal A: mark > $0.0967 AND flow >= 1.5 last 2 candles
        if mark >= RECLAIM_LEVEL and flow_now >= 1.5 and flow_prev >= 1.5:
            print(f"\n[ALERT-A] RECLAIM SIGNAL FIRED!", flush=True)
            print(f"  Mark ${mark} >= ${RECLAIM_LEVEL}", flush=True)
            print(f"  Flow now {flow_now}, prev {flow_prev} (both >= 1.5)", flush=True)
            print(f"  Suggested entry market with SL ${mark*0.985:.5f} TP ${mark*1.05:.5f}", flush=True)
            break

        # Sweep + reclaim signal B
        # If mark goes below sweep_low then comes back above $0.094 within 15 min = failed breakdown
        # (Simplified: check if last 5m low was below $0.094 but current is above)
        try:
            k5 = c.futures_klines(symbol=SYM, interval="5m", limit=3)
            last_low = min(float(k[3]) for k in k5)
            if last_low < SWEEP_LOW_LEVEL and mark > SWEEP_LOW_LEVEL + 0.0010:
                print(f"\n[ALERT-B] FAILED BREAKDOWN!", flush=True)
                print(f"  Recent 5m low ${last_low} swept ${SWEEP_LOW_LEVEL}, now back at ${mark}", flush=True)
                print(f"  Suggested entry market with SL ${SWEEP_LOW_LEVEL-0.0010:.5f}", flush=True)
                break
        except:
            pass

        # Timeout 6h
        if time.time() - start > 21600:
            print(f"\n[TIMEOUT 6h] No reclaim signal. EDEN watch ended.", flush=True)
            break

        time.sleep(15)
    except KeyboardInterrupt:
        print(f"\n[STOPPED]", flush=True)
        sys.exit(0)
    except Exception as e:
        print(f"  ERR {str(e)[:60]}", flush=True)
        time.sleep(20)
