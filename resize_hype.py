# PHASE00_LEGACY_DIRECT_RUN_GUARD
if __name__ == "__main__":
    from legacy_live_blocker import block_file_if_legacy as _phase00_block_file
    _phase00_block_file(__file__, "direct_exec")

from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance import futures as bf
from tradingagents.binance.client import spot_client
from decimal import Decimal

c = spot_client()
SYM = "HYPEUSDT"

# Step 1: close current position
print("Step 1: closing current HYPE position...")
try:
    c.futures_cancel_all_open_orders(symbol=SYM)
    print("  Orders cancelled")
except: pass
positions = c.futures_position_information(symbol=SYM)
for p in positions:
    qty = float(p["positionAmt"])
    if abs(qty) > 0:
        try:
            r = c.futures_create_order(symbol=SYM, side="SELL", type="MARKET",
                                        quantity=abs(qty), reduceOnly="true")
            print(f"  Closed: {r.get('orderId')}")
        except Exception as e:
            print(f"  Close fail: {e}")
import time
time.sleep(2)  # wait for fills + cancellation propagation

# Step 2: reopen with bigger margin
t = c.futures_symbol_ticker(symbol=SYM)
mark = float(t["price"])
print(f"\nStep 2: HYPE mark ${mark}")
print("Opening LONG margin=$2.50 lev=35x...")
try:
    res = bf.open_long(SYM, 2.50, leverage=35, isolated=True)
    print(f"OPENED qty={res.executed_qty}")
except Exception as e:
    print(f"FAIL: {e}")
    raise

info = c.futures_exchange_info()
sym_info = next(s for s in info["symbols"] if s["symbol"] == SYM)
tick = next(Decimal(f["tickSize"]) for f in sym_info["filters"]
            if f["filterType"] == "PRICE_FILTER")

positions = c.futures_position_information(symbol=SYM)
for p in positions:
    qty = abs(float(p["positionAmt"]))
    if qty <= 0:
        continue
    entry = Decimal(p["entryPrice"])
    liq = float(p["liquidationPrice"])
    sl_raw = Decimal("54.00")
    sl_floor = Decimal(str(liq)) * Decimal("1.003")
    sl = max(sl_raw, sl_floor).quantize(tick)
    tp = Decimal("57.00").quantize(tick)
    print(f"Entry ${entry}  Liq ${liq}")
    print(f"SL=${sl}  TP=${tp}")
    notional = float(qty) * float(entry)
    print(f"Notional: ${notional:.2f}")
    print(f"Max loss at SL: ${(float(sl)-float(entry))*float(qty):+.4f}")
    print(f"Max gain at TP: ${(float(tp)-float(entry))*float(qty):+.4f}")
    try:
        c.futures_create_order(symbol=SYM, side="SELL", type="STOP_MARKET",
            stopPrice=format(sl, "f"), quantity=qty,
            reduceOnly="true", workingType="CONTRACT_PRICE")
        print("SL placed")
    except Exception as e:
        print(f"SL fail: {e}")
    try:
        c.futures_create_order(symbol=SYM, side="SELL", type="TAKE_PROFIT_MARKET",
            stopPrice=format(tp, "f"), quantity=qty,
            reduceOnly="true", workingType="CONTRACT_PRICE")
        print("TP placed")
    except Exception as e:
        print(f"TP fail: {e}")

bal = c.futures_account_balance()
usdt = next(a for a in bal if a["asset"] == "USDT")
print(f"\nWallet ${float(usdt['balance']):.4f}  Avail ${float(usdt['availableBalance']):.4f}")
