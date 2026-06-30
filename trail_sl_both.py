"""Trailing SL monitor cho HYPE + EDEN.
Khi position fill, watch unrealized PnL milestones:
- Stage 2 (+33% TP): SL → true BE (entry × 1.001)
- Stage 3 (+50% TP): SL → lock $0.30-0.50 min profit
- Stage 4 (+75% TP): SL → lock $1+ min profit
"""
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

POSITIONS = {
    "HYPEUSDT": {
        "entry": 58.90,
        "sl_init": 58.30,
        "tp": 60.50,
        "qty": "1.53",
        "stages": [
            # (profit_milestone_usd, new_sl_price, label)
            (0.82, 58.96, "Stage2: true BE +0.1%"),  # 33% of $2.45 TP
            (1.22, 59.20, "Stage3: lock +$0.46"),     # 50% of TP
            (1.84, 59.50, "Stage4: lock +$0.92"),     # 75% of TP
        ],
    },
    "EDENUSDT": {
        "entry": 0.0967,
        "sl_init": 0.0950,
        "tp": 0.1020,
        "qty": "414",
        "stages": [
            (0.73, 0.0968, "Stage2: true BE"),
            (1.10, 0.0974, "Stage3: lock +$0.29"),
            (1.64, 0.0980, "Stage4: lock +$0.54"),
        ],
    },
}

# Get tick sizes
info = c.futures_exchange_info()
ticks = {}
for sym in POSITIONS:
    sym_info = next(s for s in info["symbols"] if s["symbol"]==sym)
    ticks[sym] = Decimal(next(f["tickSize"] for f in sym_info["filters"] if f["filterType"]=="PRICE_FILTER"))

state = {sym: {"current_stage": -1, "filled": False} for sym in POSITIONS}

print(f"=== Trail SL Monitor (HYPE + EDEN) ===", flush=True)
print(f"Will trail SL upward as profit milestones hit\n", flush=True)

start = time.time()
last_print = 0

while True:
    try:
        any_active = False
        any_filled = False
        for sym, cfg in POSITIONS.items():
            pos_list = c.futures_position_information(symbol=sym)
            pos = next((p for p in pos_list if abs(float(p["positionAmt"]))>0), None)

            if pos is None:
                if state[sym]["filled"]:
                    # Was filled but now closed
                    print(f"[{sym}] CLOSED (SL or TP hit)", flush=True)
                    state[sym]["filled"] = False
                continue

            any_filled = True
            any_active = True
            if not state[sym]["filled"]:
                state[sym]["filled"] = True
                entry = float(pos["entryPrice"])
                qty = abs(float(pos["positionAmt"]))
                print(f"\n[{sym} FILLED] Entry ${entry}, qty {qty}", flush=True)

            mark = float(pos["markPrice"])
            unr = float(pos["unRealizedProfit"])

            # Check next stage
            for i, (milestone, new_sl, label) in enumerate(cfg["stages"]):
                if i <= state[sym]["current_stage"]:
                    continue
                if unr >= milestone:
                    # Trigger this stage
                    print(f"\n[{sym} STAGE {i+2}] Profit ${unr:.3f} >= ${milestone}", flush=True)
                    print(f"  {label}", flush=True)
                    # Cancel old SL + place new
                    try:
                        c.futures_cancel_all_open_orders(symbol=sym)
                    except: pass
                    time.sleep(0.5)
                    sl_q = Decimal(str(new_sl)).quantize(ticks[sym])
                    tp_q = Decimal(str(cfg["tp"])).quantize(ticks[sym])
                    try:
                        c.futures_create_order(
                            symbol=sym, side="SELL", type="STOP_MARKET",
                            stopPrice=format(sl_q, "f"), quantity=cfg["qty"],
                            reduceOnly="true", workingType="MARK_PRICE"
                        )
                        print(f"  [OK] New SL @ ${sl_q}", flush=True)
                    except Exception as e:
                        print(f"  [FAIL] SL: {str(e)[:100]}", flush=True)
                    try:
                        c.futures_create_order(
                            symbol=sym, side="SELL", type="TAKE_PROFIT_MARKET",
                            stopPrice=format(tp_q, "f"), quantity=cfg["qty"],
                            reduceOnly="true", workingType="MARK_PRICE"
                        )
                        print(f"  [OK] TP re-placed @ ${tp_q}", flush=True)
                    except Exception as e:
                        print(f"  [FAIL] TP: {str(e)[:100]}", flush=True)
                    state[sym]["current_stage"] = i
                    break

            # Periodic status
            if time.time() - last_print > 60 and any_active:
                stage = state[sym]["current_stage"] + 2 if state[sym]["current_stage"] >= 0 else "init"
                print(f"  [{sym}] mark ${mark:.5f} unr ${unr:+.3f} stage={stage}", flush=True)

        if time.time() - last_print > 60:
            last_print = time.time()

        # If no active position after >2 min and not yet filled = continue waiting
        # If was filled but now closed = exit
        if not any_active and any(state[s]["filled"] for s in POSITIONS) is False:
            # Still waiting for fill
            pass
        elif not any_active:
            # All previously-filled positions closed
            print("\n[ALL CLOSED] Exiting trail monitor", flush=True)
            break

        time.sleep(20)
    except KeyboardInterrupt:
        print("\n[STOPPED]", flush=True)
        sys.exit(0)
    except Exception as e:
        print(f"  ERR {str(e)[:80]}", flush=True)
        time.sleep(25)
