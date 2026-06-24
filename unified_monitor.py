"""UNIFIED monitor: HYPE + EDEN.
1. Wait fill
2. Place initial SL/TP
3. Trail SL up on profit milestones
4. Exit when both positions closed

Stages (per coin):
- Stage 1: initial SL on fill
- Stage 2 (+33% TP): SL → true BE
- Stage 3 (+50% TP): SL → lock min profit
- Stage 4 (+75% TP): SL → lock bigger profit
"""
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
from decimal import Decimal
import time, sys

c = spot_client()

POSITIONS = {
    "MMTUSDT": {
        "entry": 0.1745,
        "sl_init": 0.1718,   # below 5m pullback base
        "tp": 0.1780,        # shark pump target area / near high retest
        "qty": "80",        # tiny wallet: ~$14 notional, 20x isolated
        "stages": [
            # qty 80, TP distance $0.0035 = max profit ~$0.28
            (0.09, 0.1748, "BE+ tiny lock"),      # mark ~$0.1756
            (0.18, 0.1760, "lock +$0.12"),        # mark ~$0.1768
            (0.24, 0.1770, "lock +$0.20"),        # mark ~$0.1775
        ],
    },
}

info = c.futures_exchange_info()
ticks = {sym: Decimal(next(f["tickSize"] for f in next(s for s in info["symbols"] if s["symbol"]==sym)["filters"] if f["filterType"]=="PRICE_FILTER")) for sym in POSITIONS}

# State machine per symbol: NEW → FILLED (initial SL/TP placed) → STAGE2 → STAGE3 → STAGE4 → CLOSED
state = {sym: {"phase": "NEW", "stage": 0} for sym in POSITIONS}

# Check existing positions at startup - if already filled, skip initial SL placement
print("Startup position check:", flush=True)
for sym in POSITIONS:
    pos_list = c.futures_position_information(symbol=sym)
    pos = next((p for p in pos_list if abs(float(p["positionAmt"]))>0), None)
    if pos:
        state[sym]["phase"] = "FILLED"
        print(f"  {sym} already has position - skip initial SL/TP placement", flush=True)
    else:
        print(f"  {sym} no position yet - waiting for entry trigger", flush=True)
print("", flush=True)

def place_sl(sym, sl_price, qty, label=""):
    sl_q = Decimal(str(sl_price)).quantize(ticks[sym])
    try:
        c.futures_create_order(symbol=sym, side="SELL", type="STOP_MARKET",
            stopPrice=format(sl_q, "f"), quantity=qty,
            reduceOnly="true", workingType="MARK_PRICE")
        print(f"  [OK] {sym} SL placed @ ${sl_q} {label}", flush=True)
        return True
    except Exception as e:
        print(f"  [FAIL] {sym} SL: {str(e)[:80]}", flush=True)
        return False

def place_tp(sym, tp_price, qty):
    tp_q = Decimal(str(tp_price)).quantize(ticks[sym])
    try:
        c.futures_create_order(symbol=sym, side="SELL", type="TAKE_PROFIT_MARKET",
            stopPrice=format(tp_q, "f"), quantity=qty,
            reduceOnly="true", workingType="MARK_PRICE")
        print(f"  [OK] {sym} TP placed @ ${tp_q}", flush=True)
        return True
    except Exception as e:
        print(f"  [FAIL] {sym} TP: {str(e)[:80]}", flush=True)
        return False

print(f"=== UNIFIED MONITOR (HYPE + EDEN) ===", flush=True)
print(f"Initial SL/TP placed on fill. Trail SL on profit milestones.\n", flush=True)

start = time.time()
last_print = 0
ever_filled = {sym: False for sym in POSITIONS}

while True:
    try:
        all_done = True
        for sym, cfg in POSITIONS.items():
            pos_list = c.futures_position_information(symbol=sym)
            pos = next((p for p in pos_list if abs(float(p["positionAmt"]))>0), None)

            if pos is None:
                if ever_filled[sym] and state[sym]["phase"] != "CLOSED":
                    print(f"\n[{sym} CLOSED]", flush=True)
                    state[sym]["phase"] = "CLOSED"
                if state[sym]["phase"] != "CLOSED":
                    all_done = False
                continue

            # Position open
            all_done = False
            mark = float(pos["markPrice"])
            unr = float(pos["unRealizedProfit"])
            entry = float(pos["entryPrice"])

            if state[sym]["phase"] == "NEW":
                # Just filled - place initial SL + TP
                print(f"\n[{sym} FILLED] Entry ${entry}, qty {pos['positionAmt']}", flush=True)
                place_sl(sym, cfg["sl_init"], cfg["qty"], "initial")
                place_tp(sym, cfg["tp"], cfg["qty"])
                state[sym]["phase"] = "FILLED"
                ever_filled[sym] = True

            elif state[sym]["phase"] == "FILLED" or state[sym]["phase"].startswith("STAGE"):
                # Check for stage progression
                for i, (milestone, new_sl, label) in enumerate(cfg["stages"]):
                    if i < state[sym]["stage"]:
                        continue
                    if unr >= milestone:
                        # Trigger stage i+2
                        print(f"\n[{sym} STAGE {i+2}] Profit ${unr:+.3f} >= ${milestone}: {label}", flush=True)
                        # Place new tighter SL (don't cancel old - old fails benignly when triggered)
                        place_sl(sym, new_sl, cfg["qty"], f"stage{i+2}")
                        state[sym]["stage"] = i + 1
                        state[sym]["phase"] = f"STAGE{i+2}"
                        break

        if time.time() - last_print > 60:
            ts = time.strftime("%H:%M:%S")
            status = []
            for sym in POSITIONS:
                pl = c.futures_position_information(symbol=sym)
                p = next((x for x in pl if abs(float(x["positionAmt"]))>0), None)
                if p:
                    status.append(f"{sym}=${float(p['markPrice']):.5f}/u${float(p['unRealizedProfit']):+.2f}/{state[sym]['phase']}")
                else:
                    if ever_filled[sym]:
                        status.append(f"{sym}=CLOSED")
                    else:
                        try:
                            m = float(c.futures_mark_price(symbol=sym)["markPrice"])
                            entry_diff = (cfg["entry"] - m) / m * 100
                            status.append(f"{sym}=${m:.5f}/waiting/{POSITIONS[sym]['entry']:.4f}({entry_diff:+.1f}%)")
                        except: pass
            print(f"[{ts}] {' | '.join(status)}", flush=True)
            last_print = time.time()

        if all_done and all(state[s]["phase"]=="CLOSED" for s in POSITIONS):
            print(f"\n[ALL CLOSED] Exiting", flush=True)
            break

        # Timeout 8h
        if time.time() - start > 28800:
            print(f"\n[TIMEOUT 8h] Exiting monitor", flush=True)
            break

        # Fast loop: 2s when waiting for fill or freshly filled, 10s when stable
        any_pos_open = any(
            ever_filled[s] and state[s]["phase"] not in ("CLOSED",)
            for s in POSITIONS
        )
        sleep_dur = 2 if not any_pos_open else (3 if any(state[s]["phase"]=="FILLED" for s in POSITIONS) else 10)
        time.sleep(sleep_dur)
    except KeyboardInterrupt:
        print("\n[STOPPED]", flush=True)
        sys.exit(0)
    except Exception as e:
        print(f"  ERR {str(e)[:80]}", flush=True)
        time.sleep(10)
