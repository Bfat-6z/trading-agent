"""Place HYPE LONG breakout limit order @ $58.90.
After fill, set SL $58.30 + TP $60.50.
Sizing: $1.5 margin × 30x = $45 notional, SL loss ~$0.46 (within $0.50 limit).
"""
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
from decimal import Decimal
import time

c = spot_client()
SYM = "HYPEUSDT"

# Sizing
MARGIN_USD = 1.5
LEVERAGE = 30
ENTRY_TRIGGER = 58.90  # break $58.90 = enter
SL_PRICE = 58.30        # below breakout retest
TP_PRICE = 60.50        # halfway to ATH $62.24

# Get tick + step
info = c.futures_exchange_info()
sym_info = next(s for s in info["symbols"] if s["symbol"]==SYM)
tick = Decimal(next(f["tickSize"] for f in sym_info["filters"] if f["filterType"]=="PRICE_FILTER"))
step = Decimal(next(f["stepSize"] for f in sym_info["filters"] if f["filterType"]=="LOT_SIZE"))
print(f"Tick: {tick}  Step: {step}")

# Calc qty
notional = MARGIN_USD * LEVERAGE
qty_raw = Decimal(str(notional)) / Decimal(str(ENTRY_TRIGGER))
qty = qty_raw.quantize(step)
print(f"\nNotional: ${notional}")
print(f"Qty: {qty}")
print(f"Entry trigger: ${ENTRY_TRIGGER}")
print(f"SL: ${SL_PRICE}  ({(SL_PRICE/ENTRY_TRIGGER-1)*100:+.2f}%)")
print(f"TP: ${TP_PRICE}  ({(TP_PRICE/ENTRY_TRIGGER-1)*100:+.2f}%)")
sl_loss = float(qty) * (ENTRY_TRIGGER - SL_PRICE)
tp_gain = float(qty) * (TP_PRICE - ENTRY_TRIGGER)
print(f"\nMax loss if SL hit: ${sl_loss:.3f}")
print(f"Profit if TP hit: ${tp_gain:.3f}")
print(f"R:R = {tp_gain/sl_loss:.2f}")

# Step 1: cancel any existing orders
try:
    c.futures_cancel_all_open_orders(symbol=SYM)
    print("\nCancelled existing orders")
except Exception as e:
    print(f"Cancel: {e}")
time.sleep(1)

# Step 2: set leverage
try:
    c.futures_change_leverage(symbol=SYM, leverage=LEVERAGE)
    print(f"Leverage set to {LEVERAGE}x")
except Exception as e:
    print(f"Lev: {e}")

# Step 3: place STOP_MARKET BUY (trigger on breakout)
trigger_q = Decimal(str(ENTRY_TRIGGER)).quantize(tick)
try:
    order = c.futures_create_order(
        symbol=SYM,
        side="BUY",
        type="STOP_MARKET",
        stopPrice=format(trigger_q, "f"),
        quantity=str(qty),
        workingType="MARK_PRICE",  # use mark, not last (avoid wick triggers)
        timeInForce="GTC"
    )
    print(f"\n[OK] Entry STOP_MARKET BUY placed: trigger ${trigger_q}, qty {qty}")
    print(f"     Order ID: {order.get('orderId')}")
except Exception as e:
    print(f"\n[FAIL] Entry order: {e}")
    raise

print(f"\n=== ORDER PLACED ===")
print(f"WAIT: HYPE break ${ENTRY_TRIGGER} → auto BUY {qty} HYPE")
print(f"After fill: need to manually run SL/TP placement script")
print(f"Or cancel if no fill in 4h")
