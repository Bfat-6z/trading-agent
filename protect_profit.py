"""Move SL up to lock profit + add partial TP at +4%."""
# PHASE00_LEGACY_DIRECT_RUN_GUARD
if __name__ == "__main__":
    from legacy_live_blocker import block_file_if_legacy as _phase00_block_file
    _phase00_block_file(__file__, "direct_exec")

from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
import time

c = spot_client()
SYMBOL = "MAGMAUSDT"

pos = c.futures_position_information(symbol=SYMBOL)[0]
qty = abs(float(pos["positionAmt"]))
entry = float(pos["entryPrice"])
mark = float(pos["markPrice"])
unpnl = float(pos["unRealizedProfit"])
print(f"Position: {qty} MAGMA  entry=${entry}  mark=${mark}  unPnL=${unpnl:+.4f}")

# Cancel existing algo orders first
print("\nCurrent algo orders:")
algo = c._request_futures_api("get", "openAlgoOrders", True, data={"symbol": SYMBOL})
for o in algo:
    print(f"  {o['orderType']:<22} trigger=${o['triggerPrice']}  algoId={o['algoId']}")

# Cancel all via cancel_all (works for regular, may not work for algo — user manual if needed)
try:
    res = c._request_futures_api("delete", "allOpenOrders", True, data={"symbol": SYMBOL})
    print(f"Cancel all: {res}")
except Exception as e:
    print(f"Cancel all failed: {e}")
time.sleep(2)

# Place new orders:
# - SL moved up to entry * 1.005 (lock +0.5% min, basically breakeven after fees)
# - TP1 at +3.5% (partial 50%)
# - TP2 at +7% (remaining 50%)
sl_price = round(entry * 1.005, 5)   # +0.5% above entry = lock breakeven
tp1_price = round(entry * 1.035, 5)
tp2_price = round(entry * 1.07, 5)
half_qty = qty / 2

print(f"\nPlacing trailing SL @ ${sl_price} (lock breakeven)")
sl = c.futures_create_order(
    symbol=SYMBOL, side="SELL", type="STOP_MARKET",
    stopPrice=str(sl_price), quantity=qty, reduceOnly="true",
)
print(f"  algoId={sl.get('algoId')} status={sl.get('algoStatus')}")

time.sleep(1)
print(f"Placing TP1 @ ${tp1_price} (sell {half_qty} = 50%)")
tp1 = c.futures_create_order(
    symbol=SYMBOL, side="SELL", type="TAKE_PROFIT_MARKET",
    stopPrice=str(tp1_price), quantity=half_qty, reduceOnly="true",
)
print(f"  algoId={tp1.get('algoId')} status={tp1.get('algoStatus')}")

time.sleep(1)
print(f"Placing TP2 @ ${tp2_price} (sell remaining {half_qty})")
tp2 = c.futures_create_order(
    symbol=SYMBOL, side="SELL", type="TAKE_PROFIT_MARKET",
    stopPrice=str(tp2_price), quantity=half_qty, reduceOnly="true",
)
print(f"  algoId={tp2.get('algoId')} status={tp2.get('algoStatus')}")

time.sleep(2)
final = c._request_futures_api("get", "openAlgoOrders", True, data={"symbol": SYMBOL})
print(f"\nFinal algo orders: {len(final)}")
for o in final:
    print(f"  {o['orderType']:<22} trigger=${o['triggerPrice']}  qty={o['quantity']}")
