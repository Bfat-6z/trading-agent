"""Execute GMT LONG snip: market BUY + SL + TP."""
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
from decimal import Decimal
import time
c = spot_client()
SYM = "GMTUSDT"

MARGIN = 1.0
LEVERAGE = 50
SL_PRICE = 0.01275
TP_PRICE = 0.01340

# Tick/step
info = c.futures_exchange_info()
sym_info = next(s for s in info["symbols"] if s["symbol"]==SYM)
tick = Decimal(next(f["tickSize"] for f in sym_info["filters"] if f["filterType"]=="PRICE_FILTER"))
step = Decimal(next(f["stepSize"] for f in sym_info["filters"] if f["filterType"]=="LOT_SIZE"))
print(f"Tick: {tick}  Step: {step}")

# Current price
mark = float(c.futures_mark_price(symbol=SYM)["markPrice"])
print(f"Current mark: ${mark:.5f}")

# Set leverage
try:
    c.futures_change_leverage(symbol=SYM, leverage=LEVERAGE)
    print(f"Leverage: {LEVERAGE}x")
except Exception as e:
    print(f"Lev err: {e}")

# Calc qty
notional = MARGIN * LEVERAGE
qty_raw = Decimal(str(notional)) / Decimal(str(mark))
qty = qty_raw.quantize(step)
print(f"Notional: ${notional}, qty {qty}")

# Market BUY
try:
    o = c.futures_create_order(
        symbol=SYM, side="BUY", type="MARKET",
        quantity=str(qty)
    )
    print(f"\n[OK] Market BUY filled: {o.get('orderId')}")
except Exception as e:
    print(f"\n[FAIL] BUY: {e}")
    raise

time.sleep(2)

# Get actual entry
pos_list = c.futures_position_information(symbol=SYM)
pos = next((p for p in pos_list if abs(float(p["positionAmt"]))>0), None)
if pos:
    entry = float(pos["entryPrice"])
    qty_real = float(pos["positionAmt"])
    print(f"\nPosition: qty {qty_real} @ entry ${entry}")

    # Place SL
    sl_q = Decimal(str(SL_PRICE)).quantize(tick)
    try:
        c.futures_create_order(symbol=SYM, side="SELL", type="STOP_MARKET",
            stopPrice=format(sl_q, "f"), quantity=str(qty),
            reduceOnly="true", workingType="MARK_PRICE")
        print(f"[OK] SL @ ${sl_q}")
    except Exception as e:
        print(f"[FAIL] SL: {e}")

    # Place TP
    tp_q = Decimal(str(TP_PRICE)).quantize(tick)
    try:
        c.futures_create_order(symbol=SYM, side="SELL", type="TAKE_PROFIT_MARKET",
            stopPrice=format(tp_q, "f"), quantity=str(qty),
            reduceOnly="true", workingType="MARK_PRICE")
        print(f"[OK] TP @ ${tp_q}")
    except Exception as e:
        print(f"[FAIL] TP: {e}")

    print(f"\n=== GMT LONG SNIP ACTIVE ===")
    sl_loss = qty_real * (entry - SL_PRICE)
    tp_gain = qty_real * (TP_PRICE - entry)
    print(f"Entry ${entry}  SL ${SL_PRICE} (-${sl_loss:.3f})  TP ${TP_PRICE} (+${tp_gain:.3f})")
    print(f"R:R = {tp_gain/sl_loss:.2f}")
