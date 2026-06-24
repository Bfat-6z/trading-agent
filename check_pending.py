"""Check for pending deposits + transfers."""
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
import time, datetime as dt

c = spot_client()
now = int(time.time() * 1000)
start_24h = now - 86400000

print("=== ALL DEPOSITS LAST 24h (any status) ===")
for status in [0, 1, 6]:  # 0=pending, 1=success, 6=credited
    try:
        deps = c.get_deposit_history(status=status, startTime=start_24h, endTime=now)
        label = {0: "PENDING", 1: "SUCCESS", 6: "CREDITED"}[status]
        for d in deps:
            ts = d.get("insertTime", 0) / 1000
            print(f"  [{label}] {dt.datetime.fromtimestamp(ts).isoformat()}: {d.get('amount')} {d.get('coin')} via {d.get('network')} (confirms {d.get('confirmTimes', 'n/a')})")
    except Exception as e:
        pass

print("\n=== WITHDRAWALS LAST 24h ===")
try:
    wd = c.get_withdraw_history(startTime=start_24h, endTime=now)
    if not wd:
        print("  (none)")
    for w in wd[:5]:
        ts = w.get("applyTime", 0)
        if isinstance(ts, str):
            print(f"  [{w.get('status')}] {ts}: {w.get('amount')} {w.get('coin')} via {w.get('network')}")
        else:
            print(f"  status={w.get('status')}: {w.get('amount')} {w.get('coin')} via {w.get('network')}")
except Exception as e:
    print(f"  Error: {e}")

print("\n=== INTERNAL TRANSFERS (Funding<->Spot) last 24h ===")
for tt in ["MAIN_FUNDING", "FUNDING_MAIN", "MAIN_UMFUTURE", "UMFUTURE_MAIN",
           "FUNDING_UMFUTURE", "UMFUTURE_FUNDING"]:
    try:
        res = c.query_universal_transfer_history(type=tt, startTime=start_24h, endTime=now)
        rows = res.get("rows", []) if isinstance(res, dict) else []
        for r in rows[:3]:
            ts = r.get("timestamp", 0) / 1000
            print(f"  [{tt}] {dt.datetime.fromtimestamp(ts).isoformat()}: {r.get('amount')} {r.get('asset')} status={r.get('status')}")
    except Exception:
        pass

# Also check pay history (Binance Pay)
print("\n=== PAY HISTORY (last 24h) ===")
try:
    pay = c.get_pay_trade_history(startTime=start_24h, endTime=now)
    rows = pay.get("data", []) if isinstance(pay, dict) else []
    if not rows:
        print("  (none)")
    for r in rows[:5]:
        print(f"  {r.get('orderType')}: {r.get('amount')} {r.get('currency')} status={r.get('transactionType')}")
except Exception as e:
    print(f"  Error: {e}")
