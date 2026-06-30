"""Monitor PROVEUSDT SHORT — emit on key state changes only."""
# PHASE00_LEGACY_DIRECT_RUN_GUARD
if __name__ == "__main__":
    from legacy_live_blocker import block_file_if_legacy as _phase00_block_file
    _phase00_block_file(__file__, "direct_exec")

from dotenv import load_dotenv
load_dotenv()
import time
from tradingagents.binance.client import spot_client

c = spot_client()
SYMBOL = "PROVEUSDT"
ENTRY = 0.3474
SL_TRIGGER = 0.3648
TP_TARGET = 0.3127

last_bucket = None       # buckets: 0=neutral, +1=+$0.5, +2=+$1.0, +3=+$1.5, +4=+$2.0
last_dd_bucket = None    # drawdown buckets: -1=-$0.30, -2=-$0.60, -3=-$0.90

def bucket(pnl):
    if pnl >= 2.0: return 4
    if pnl >= 1.5: return 3
    if pnl >= 1.0: return 2
    if pnl >= 0.5: return 1
    return 0

def dd_bucket(pnl):
    if pnl <= -0.90: return -3
    if pnl <= -0.60: return -2
    if pnl <= -0.30: return -1
    return 0

print(f"MONITOR_START PROVE_SHORT entry=${ENTRY} target=${TP_TARGET} sl=${SL_TRIGGER}", flush=True)

while True:
    try:
        positions = c.futures_position_information(symbol=SYMBOL)
        p = next((x for x in positions if abs(float(x["positionAmt"])) > 0), None)
        if p is None:
            # Position closed — check trade history for outcome
            trades = c.futures_account_trades(symbol=SYMBOL, limit=5)
            pnls = [(float(t.get("realizedPnl", 0)), float(t["price"]), t["side"])
                    for t in trades if float(t.get("realizedPnl", 0)) != 0]
            total = sum(p for p, _, _ in pnls)
            for pnl, price, side in pnls:
                print(f"CLOSE side={side} price=${price} realized=${pnl:+.4f}", flush=True)
            print(f"POSITION_CLOSED total_realized=${total:+.4f}", flush=True)
            break

        mark = float(p["markPrice"])
        pnl = float(p["unRealizedProfit"])
        liq = float(p["liquidationPrice"])
        liq_dist_pct = (liq - mark) / mark * 100

        # Profit milestones
        b = bucket(pnl)
        if last_bucket is None or b != last_bucket:
            if b > 0 and (last_bucket is None or b > last_bucket):
                print(f"PROFIT_MILESTONE +${pnl:.4f} mark=${mark} (target ${TP_TARGET})", flush=True)
            elif last_bucket is not None and b < last_bucket:
                print(f"PROFIT_GAVE_BACK now +${pnl:.4f} mark=${mark}", flush=True)
            last_bucket = b

        # Drawdown alerts
        d = dd_bucket(pnl)
        if last_dd_bucket is None or d != last_dd_bucket:
            if d < 0:
                print(f"DRAWDOWN ${pnl:.4f} mark=${mark} (SL ${SL_TRIGGER})", flush=True)
            last_dd_bucket = d

        # Liquidation proximity
        if liq_dist_pct < 3:
            print(f"LIQ_WARNING mark=${mark} liq=${liq} dist={liq_dist_pct:.1f}%", flush=True)

        time.sleep(20)
    except Exception as e:
        print(f"ERROR {type(e).__name__}: {str(e)[:80]}", flush=True)
        time.sleep(30)
