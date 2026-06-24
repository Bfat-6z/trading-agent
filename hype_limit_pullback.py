"""Set HYPE LIMIT BUY @ $61.00 - waiting for pullback.
SL $60 (max loss $0.50), TP $63.50."""
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
from decimal import Decimal
c = spot_client()
SYM = "HYPEUSDT"

MARGIN = 1.0
LEVERAGE = 30
LIMIT_PRICE = 61.00
SL_PRICE = 60.00
TP_PRICE = 63.50

# Verify current price
mark = float(c.futures_mark_price(symbol=SYM)["markPrice"])
print(f"Current mark: ${mark}")
print(f"Limit BUY at: ${LIMIT_PRICE}")
print(f"Pullback needed: {(LIMIT_PRICE/mark-1)*100:+.2f}%")

if mark < LIMIT_PRICE:
    print(f"[WARN] Mark below limit, will fill immediately. Abort.")
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

# Calc qty
notional = MARGIN * LEVERAGE
qty_raw = Decimal(str(notional)) / Decimal(str(LIMIT_PRICE))
qty = qty_raw.quantize(step)
print(f"Notional: ${notional}, qty {qty}")

# Place LIMIT BUY
limit_q = Decimal(str(LIMIT_PRICE)).quantize(tick)
try:
    o = c.futures_create_order(
        symbol=SYM, side="BUY", type="LIMIT",
        price=format(limit_q, "f"),
        quantity=str(qty),
        timeInForce="GTC"
    )
    print(f"[OK] LIMIT BUY placed: orderId {o.get('orderId')}")
except Exception as e:
    print(f"[FAIL] {e}")
    raise

print(f"\n=== HYPE PULLBACK LIMIT ORDER ACTIVE ===")
print(f"Waiting for HYPE pullback to ${LIMIT_PRICE}")
print(f"Current ${mark}, need drop ~{((mark-LIMIT_PRICE)/mark)*100:.2f}%")
print(f"On fill: monitor will auto-place SL ${SL_PRICE} + TP ${TP_PRICE}")

sl_loss = float(qty) * (LIMIT_PRICE - SL_PRICE)
tp_gain = float(qty) * (TP_PRICE - LIMIT_PRICE)
print(f"\nMath after fill:")
print(f"  SL hit: -${sl_loss:.3f}")
print(f"  TP hit: +${tp_gain:.3f}")
print(f"  R:R = {tp_gain/sl_loss:.2f}")
