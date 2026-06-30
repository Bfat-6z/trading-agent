"""EDEN LONG breakout order @ $0.0967 (reclaim level).
After fill: SL $0.0950, TP $0.1020.
Sizing $1 margin × 20x = $20 notional (max lev for EDEN)."""
# PHASE00_LEGACY_DIRECT_RUN_GUARD
if __name__ == "__main__":
    from legacy_live_blocker import block_file_if_legacy as _phase00_block_file
    _phase00_block_file(__file__, "direct_exec")

from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
from decimal import Decimal
import time

c = spot_client()
SYM = "EDENUSDT"

MARGIN_USD = 1.0
LEVERAGE = 20
ENTRY_TRIGGER = 0.0967
SL_PRICE = 0.0950
TP_PRICE = 0.1020

info = c.futures_exchange_info()
sym_info = next(s for s in info["symbols"] if s["symbol"]==SYM)
tick = Decimal(next(f["tickSize"] for f in sym_info["filters"] if f["filterType"]=="PRICE_FILTER"))
step = Decimal(next(f["stepSize"] for f in sym_info["filters"] if f["filterType"]=="LOT_SIZE"))
print(f"Tick: {tick}  Step: {step}")

notional = MARGIN_USD * LEVERAGE
qty_raw = Decimal(str(notional)) / Decimal(str(ENTRY_TRIGGER))
qty = qty_raw.quantize(step)
print(f"\nNotional: ${notional}")
print(f"Qty: {qty}")
print(f"Entry trigger: ${ENTRY_TRIGGER}")
print(f"SL: ${SL_PRICE} ({(SL_PRICE/ENTRY_TRIGGER-1)*100:+.2f}%)")
print(f"TP: ${TP_PRICE} ({(TP_PRICE/ENTRY_TRIGGER-1)*100:+.2f}%)")
sl_loss = float(qty) * (ENTRY_TRIGGER - SL_PRICE)
tp_gain = float(qty) * (TP_PRICE - ENTRY_TRIGGER)
print(f"\nMax loss if SL: ${sl_loss:.3f}")
print(f"Profit if TP: ${tp_gain:.3f}")
print(f"R:R = {tp_gain/sl_loss:.2f}")

try:
    c.futures_cancel_all_open_orders(symbol=SYM)
except Exception as e:
    print(f"Cancel: {e}")
time.sleep(1)

try:
    c.futures_change_leverage(symbol=SYM, leverage=LEVERAGE)
    print(f"Leverage set to {LEVERAGE}x")
except Exception as e:
    print(f"Lev: {e}")

trigger_q = Decimal(str(ENTRY_TRIGGER)).quantize(tick)
try:
    order = c.futures_create_order(
        symbol=SYM, side="BUY", type="STOP_MARKET",
        stopPrice=format(trigger_q, "f"),
        quantity=str(qty),
        workingType="MARK_PRICE",
        timeInForce="GTC"
    )
    print(f"\n[OK] STOP_MARKET BUY placed: trigger ${trigger_q}, qty {qty}")
except Exception as e:
    print(f"\n[FAIL] {e}")
    raise

print(f"\n=== EDEN ORDER PLACED ===")
print(f"Total exposure (both): HYPE ${1.5} + EDEN ${1.0} = ${2.5} margin")
print(f"Max combined loss: HYPE ${0.46} + EDEN ${sl_loss:.2f} = ${0.46+sl_loss:.2f}")
print(f"Max combined gain: HYPE ${1.22} + EDEN ${tp_gain:.2f} = ${1.22+tp_gain:.2f}")
