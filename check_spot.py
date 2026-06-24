from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
import time, datetime as dt

c = spot_client()
print("=== SPOT BALANCE (post-convert) ===")
acc = c.get_account()
total_usdt = 0
for b in acc["balances"]:
    f = float(b["free"]); l = float(b["locked"])
    if f + l > 0.00001:
        sym = b["asset"]
        print(f"  {sym:8s}  free={f:>15.8f}  locked={l}")
        if sym == "USDT":
            total_usdt += f
        elif sym == "ETH":
            total_usdt += f * 2120
print(f"\n  Approx total USD: ${total_usdt:.4f}")

# Convert status
print("\n=== Recent Convert Orders ===")
end = int(time.time() * 1000)
start = end - 3600 * 1000  # 1h
try:
    res = c.get_convert_trade_history(startTime=start, endTime=end)
    rows = res.get("list", []) if isinstance(res, dict) else []
    for r in rows[:5]:
        print(f"  {r.get('fromAsset')} {r.get('fromAmount')} -> {r.get('toAsset')} {r.get('toAmount')} status={r.get('orderStatus')}")
except Exception as e:
    print(f"  Error: {e}")
