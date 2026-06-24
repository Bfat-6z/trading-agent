"""SHORT ZECUSDT — privacy coin pump exhaustion play."""
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance import futures as bf
from tradingagents.binance.client import spot_client
import time

SYMBOL = "ZECUSDT"
MARGIN = 2.0
LEVERAGE = 3
SL_PCT = 4.0   # +4% above entry
TP1_PCT = 3.0  # -3% below entry, 50%
TP2_PCT = 6.0  # -6% below entry, 50%

c = spot_client()
print(f"OPERATOR override: SHORT {SYMBOL}  margin=${MARGIN}  lev={LEVERAGE}x")
bal = bf.get_futures_balance()
print(f"Avail: ${bal['available']:.4f}")

mark = float(c.futures_mark_price(symbol=SYMBOL)["markPrice"])
print(f"Mark: ${mark}")

res = bf.open_short(SYMBOL, MARGIN, leverage=LEVERAGE, isolated=True)
print(f"\nORDER: id={res.order_id}  qty={res.executed_qty}  avgPrice=${res.avg_price}")

# Get position info
time.sleep(1)
pos = c.futures_position_information(symbol=SYMBOL)[0]
qty = abs(float(pos["positionAmt"]))
entry = float(pos["entryPrice"])
print(f"Position: SHORT {qty} @ entry=${entry}  liquidation=${pos['liquidationPrice']}")

# Calculate SL/TP prices (SHORT: SL above, TP below)
sl_price = round(entry * (1 + SL_PCT / 100), 4)
tp1_price = round(entry * (1 - TP1_PCT / 100), 4)
tp2_price = round(entry * (1 - TP2_PCT / 100), 4)
half_qty = qty / 2
print(f"\nSL @ ${sl_price}  TP1 @ ${tp1_price}  TP2 @ ${tp2_price}")

# Place SL (BUY to close SHORT)
time.sleep(1)
sl = c.futures_create_order(
    symbol=SYMBOL, side="BUY", type="STOP_MARKET",
    stopPrice=str(sl_price), quantity=qty, reduceOnly="true",
)
print(f"SL placed: algoId={sl.get('algoId')}")

# TP1 (sell 50%)
time.sleep(1)
tp1 = c.futures_create_order(
    symbol=SYMBOL, side="BUY", type="TAKE_PROFIT_MARKET",
    stopPrice=str(tp1_price), quantity=half_qty, reduceOnly="true",
)
print(f"TP1 placed: algoId={tp1.get('algoId')}")

# TP2
time.sleep(1)
tp2 = c.futures_create_order(
    symbol=SYMBOL, side="BUY", type="TAKE_PROFIT_MARKET",
    stopPrice=str(tp2_price), quantity=half_qty, reduceOnly="true",
)
print(f"TP2 placed: algoId={tp2.get('algoId')}")

time.sleep(2)
final = c._request_futures_api("get", "openAlgoOrders", True, data={"symbol": SYMBOL})
print(f"\nFinal algo orders: {len(final)}")
for o in final:
    print(f"  {o['orderType']:<22} trigger=${o['triggerPrice']}  qty={o['quantity']}")
