"""Move all USDT from Spot to USDT-M Futures wallet."""
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
c = spot_client()

bal = c.get_asset_balance(asset="USDT")
amount = float(bal["free"])
print(f"Spot USDT free: ${amount:.6f}")

if amount < 1:
    print("Not enough USDT to transfer.")
    raise SystemExit(0)

# Reserve $0.01 for any pending fees, transfer the rest
move = round(amount - 0.01, 2)
print(f"Transferring ${move} to Futures USDT-M...")

# Universal transfer type: MAIN_UMFUTURE = Spot -> USDT-M Futures
res = c.make_universal_transfer(type="MAIN_UMFUTURE", asset="USDT", amount=str(move))
print(f"Result: {res}")

# Verify
import time; time.sleep(2)
fut = c.futures_account_balance()
for a in fut:
    if a.get("asset") == "USDT":
        print(f"Futures USDT balance now: ${a.get('balance')}")
        print(f"Available: ${a.get('availableBalance')}")
        break

spot_bal = c.get_asset_balance(asset="USDT")
print(f"Spot USDT remaining: ${spot_bal['free']}")
