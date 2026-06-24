"""Close ALGOUSDT short at market — lock current profit."""
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
import time

c = spot_client()
SYMBOL = "ALGOUSDT"

pos = c.futures_position_information(symbol=SYMBOL)[0]
qty = abs(float(pos["positionAmt"]))
entry = float(pos["entryPrice"])
mark = float(pos["markPrice"])
unpnl = float(pos["unRealizedProfit"])
print(f"Closing SHORT {qty} ALGO  entry=${entry}  mark=${mark}  unPnL=${unpnl:+.4f}")

# Close = BUY (opposite of SHORT)
try:
    res = c.futures_create_order(
        symbol=SYMBOL, side="BUY", type="MARKET", quantity=qty, reduceOnly="true",
    )
    print(f"Close response: {res}")
except Exception as e:
    print(f"Close failed: {e}")
    raise

time.sleep(2)
# Verify
pos_after = c.futures_position_information(symbol=SYMBOL)[0]
qty_after = float(pos_after["positionAmt"])
print(f"\nPosition after: qty={qty_after}")

# Final balance
bal = c.futures_account_balance()
for a in bal:
    if a["asset"] == "USDT":
        print(f"Futures USDT balance: ${a['balance']}  avail: ${a['availableBalance']}")
        break

# Cancel any leftover orders
try:
    c.futures_cancel_all_open_orders(symbol=SYMBOL)
    print("Cancelled remaining orders")
except Exception:
    pass
