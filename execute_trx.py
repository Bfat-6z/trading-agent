"""Execute TRX LONG A+ volatility squeeze breakout."""
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
SYM = "TRXUSDT"

MARGIN = 1.5
LEVERAGE = 25
SL_PRICE = 0.3608
TP_PRICE = 0.3678  # TP1 conservative

mark = float(c.futures_mark_price(symbol=SYM)["markPrice"])
print(f"Current mark: ${mark:.5f}")

if mark > 0.3650:
    print(f"[WARN] Price extended beyond entry zone $0.3640. Skip to avoid chase.")
    exit(0)

# Tick/step
info = c.futures_exchange_info()
sym_info = next(s for s in info["symbols"] if s["symbol"]==SYM)
tick = Decimal(next(f["tickSize"] for f in sym_info["filters"] if f["filterType"]=="PRICE_FILTER"))
step = Decimal(next(f["stepSize"] for f in sym_info["filters"] if f["filterType"]=="LOT_SIZE"))
print(f"Tick: {tick}  Step: {step}")

try:
    c.futures_change_leverage(symbol=SYM, leverage=LEVERAGE)
    print(f"Leverage: {LEVERAGE}x")
except Exception as e:
    print(f"Lev err: {e}")

notional = MARGIN * LEVERAGE
qty_raw = Decimal(str(notional)) / Decimal(str(mark))
qty = qty_raw.quantize(step)
print(f"Notional: ${notional}, qty {qty}")

try:
    o = c.futures_create_order(
        symbol=SYM, side="BUY", type="MARKET",
        quantity=str(qty)
    )
    print(f"[OK] Market BUY: {o.get('orderId')}")
except Exception as e:
    print(f"[FAIL] BUY: {e}")
    raise

time.sleep(2)

pos_list = c.futures_position_information(symbol=SYM)
pos = next((p for p in pos_list if abs(float(p["positionAmt"]))>0), None)
if pos:
    entry = float(pos["entryPrice"])
    qty_real = float(pos["positionAmt"])
    print(f"\nFilled: qty {qty_real} @ ${entry}")

    sl_q = Decimal(str(SL_PRICE)).quantize(tick)
    tp_q = Decimal(str(TP_PRICE)).quantize(tick)

    try:
        c.futures_create_order(symbol=SYM, side="SELL", type="STOP_MARKET",
            stopPrice=format(sl_q, "f"), quantity=str(qty),
            reduceOnly="true", workingType="MARK_PRICE")
        print(f"[OK] SL @ ${sl_q}")
    except Exception as e:
        print(f"[FAIL] SL: {e}")

    try:
        c.futures_create_order(symbol=SYM, side="SELL", type="TAKE_PROFIT_MARKET",
            stopPrice=format(tp_q, "f"), quantity=str(qty),
            reduceOnly="true", workingType="MARK_PRICE")
        print(f"[OK] TP @ ${tp_q}")
    except Exception as e:
        print(f"[FAIL] TP: {e}")

    sl_loss = qty_real * (entry - SL_PRICE)
    tp_gain = qty_real * (TP_PRICE - entry)
    print(f"\n=== TRX LONG ACTIVE ===")
    print(f"Entry ${entry}  SL ${SL_PRICE} (-${sl_loss:.3f})  TP ${TP_PRICE} (+${tp_gain:.3f})")
    print(f"R:R = {tp_gain/sl_loss:.2f}")
