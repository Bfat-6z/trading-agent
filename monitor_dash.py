"""Monitor DASH LONG — $1 net target."""
from dotenv import load_dotenv
load_dotenv()
import time
from tradingagents.binance.client import spot_client
c = spot_client()
SYMBOL = "DASHUSDT"
ENTRY = 49.07
SL = 47.60
TP = 52.01
last_b = None; last_dd = None
def b(p):
    if p >= 1.0: return 2
    if p >= 0.5: return 1
    return 0
def dd(p):
    if p <= -0.40: return -2
    if p <= -0.20: return -1
    return 0
print(f"MONITOR DASH_LONG entry=${ENTRY} TP=${TP} SL=${SL}", flush=True)
while True:
    try:
        positions = c.futures_position_information(symbol=SYMBOL)
        p = next((x for x in positions if abs(float(x["positionAmt"])) > 0), None)
        if p is None:
            trades = c.futures_account_trades(symbol=SYMBOL, limit=5)
            pnls = [(float(t.get("realizedPnl", 0)), float(t["price"]), t["side"])
                    for t in trades if float(t.get("realizedPnl", 0)) != 0]
            total = sum(x for x, _, _ in pnls)
            for pnl, price, side in pnls:
                print(f"CLOSE {side} ${price} realized=${pnl:+.4f}", flush=True)
            print(f"POSITION_CLOSED total=${total:+.4f}", flush=True)
            break
        mark = float(p["markPrice"])
        pnl = float(p["unRealizedProfit"])
        new_b = b(pnl)
        if last_b is None or new_b != last_b:
            if new_b > 0 and (last_b is None or new_b > last_b):
                print(f"PROFIT +${pnl:.4f} mark=${mark}", flush=True)
            elif last_b is not None and new_b < last_b:
                print(f"GAVE_BACK +${pnl:.4f} mark=${mark}", flush=True)
            last_b = new_b
        new_dd = dd(pnl)
        if last_dd is None or new_dd != last_dd:
            if new_dd < 0:
                print(f"DD ${pnl:.4f} mark=${mark}", flush=True)
            last_dd = new_dd
        time.sleep(15)
    except Exception as e:
        print(f"ERR {type(e).__name__}: {str(e)[:80]}", flush=True)
        time.sleep(20)
