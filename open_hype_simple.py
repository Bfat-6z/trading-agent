from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
from decimal import Decimal
import time

c = spot_client()
SYM = "HYPEUSDT"

# Cancel all + wait for propagation
print("Step 1: cancel all orders + wait")
try:
    c.futures_cancel_all_open_orders(symbol=SYM)
except: pass
time.sleep(5)

# Check no orders
algos = c._request_futures_api("get", "openAlgoOrders", True, data={"symbol": SYM})
n = len(algos) if isinstance(algos, list) else 0
print(f"  Algo orders left: {n}")

# Direct open without margin-type change
print("\nStep 2: set leverage 35x")
try:
    c.futures_change_leverage(symbol=SYM, leverage=35)
    print("  Leverage set")
except Exception as e:
    print(f"  Leverage: {e}")

print("\nStep 3: Place market BUY (qty = margin*lev / price)")
t = c.futures_symbol_ticker(symbol=SYM)
mark = float(t["price"])
margin = 2.50
lev = 35
notional = margin * lev
# Lot size
info = c.futures_exchange_info()
sym_info = next(s for s in info["symbols"] if s["symbol"] == SYM)
step = next(Decimal(f["stepSize"]) for f in sym_info["filters"]
            if f["filterType"] == "LOT_SIZE")
qty_raw = Decimal(str(notional / mark))
qty = (qty_raw // step) * step
qty_str = format(qty, "f")
print(f"  Mark ${mark}  Notional ${notional}  Qty {qty_str}")

try:
    r = c.futures_create_order(symbol=SYM, side="BUY", type="MARKET",
                                quantity=qty_str)
    print(f"  OPENED: {r.get('orderId')}")
except Exception as e:
    print(f"  Open fail: {e}")
    raise

time.sleep(1)
# Place SL+TP
tick = next(Decimal(f["tickSize"]) for f in sym_info["filters"]
            if f["filterType"] == "PRICE_FILTER")
positions = c.futures_position_information(symbol=SYM)
for p in positions:
    qty_p = abs(float(p["positionAmt"]))
    if qty_p <= 0: continue
    entry = Decimal(p["entryPrice"])
    liq = float(p["liquidationPrice"])
    sl = max(Decimal("54.00"), Decimal(str(liq)) * Decimal("1.003")).quantize(tick)
    tp = Decimal("57.00").quantize(tick)
    print(f"\nEntry ${entry}  Liq ${liq}")
    print(f"SL=${sl}  TP=${tp}")
    print(f"Max loss: ${(float(sl)-float(entry))*qty_p:+.4f}")
    print(f"Max gain: ${(float(tp)-float(entry))*qty_p:+.4f}")
    try:
        c.futures_create_order(symbol=SYM, side="SELL", type="STOP_MARKET",
            stopPrice=format(sl, "f"), quantity=qty_p,
            reduceOnly="true", workingType="CONTRACT_PRICE")
        print("SL placed")
    except Exception as e:
        print(f"SL fail: {e}")
    try:
        c.futures_create_order(symbol=SYM, side="SELL", type="TAKE_PROFIT_MARKET",
            stopPrice=format(tp, "f"), quantity=qty_p,
            reduceOnly="true", workingType="CONTRACT_PRICE")
        print("TP placed")
    except Exception as e:
        print(f"TP fail: {e}")

bal = c.futures_account_balance()
usdt = next(a for a in bal if a["asset"] == "USDT")
print(f"\nWallet ${float(usdt['balance']):.4f}  Avail ${float(usdt['availableBalance']):.4f}")
