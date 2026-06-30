"""Close MAGMA long at market. Lock current profit."""
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
print(f"Closing: {qty} MAGMA  entry=${entry}  mark=${mark}  unPnL=${unpnl:+.4f}")

if qty == 0:
    print("Already closed")
    raise SystemExit(0)

# Close at market
res = c.futures_create_order(
    symbol=SYMBOL, side="SELL", type="MARKET",
    quantity=qty, reduceOnly="true",
)
print(f"\nClose order: id={res.get('orderId')} status={res.get('status')}")

time.sleep(2)
# Verify
pos_after = c.futures_position_information(symbol=SYMBOL)
if pos_after and abs(float(pos_after[0]["positionAmt"])) > 0:
    print(f"WARNING: position still open: {pos_after[0]['positionAmt']}")
else:
    print("Position CLOSED")

# Cancel all remaining algo orders
try:
    c._request_futures_api("delete", "allOpenOrders", True, data={"symbol": SYMBOL})
    print("Cancelled remaining orders")
except Exception as e:
    print(f"Cancel error: {e}")

# Final balance
bal = c.futures_account_balance()
for a in bal:
    if a["asset"] == "USDT":
        print(f"\nFutures USDT: ${a['balance']}  avail: ${a['availableBalance']}")
        break
