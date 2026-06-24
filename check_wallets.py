"""Check where the deposited ETH went."""
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
import time, datetime as dt

c = spot_client()

# Full deposit history with details
print("=== DEPOSITS (Last 10) ===")
try:
    deps = c.get_deposit_history()
    for d in deps[:10]:
        ts = d.get("insertTime", 0) / 1000
        dt_str = dt.datetime.fromtimestamp(ts).isoformat()
        print(f"  [{dt_str}] {d.get('coin')}: {d.get('amount')} via {d.get('network')}")
        print(f"      tx: {d.get('txId', 'n/a')[:30]}...")
        print(f"      status: {d.get('status')}  (1=success)")
        print(f"      walletType: {d.get('walletType', 'n/a')}")
except Exception as e:
    print(f"  Error: {e}")

# Convert history
print("\n=== CONVERT HISTORY (last 7d) ===")
try:
    end = int(time.time() * 1000)
    start = end - 7 * 86400000
    res = c.get_convert_trade_history(startTime=start, endTime=end)
    rows = res.get("list", []) if isinstance(res, dict) else []
    if not rows:
        print("  (no conversions)")
    for r in rows[:10]:
        print(f"  {r.get('fromAsset')} {r.get('fromAmount')} -> {r.get('toAsset')} {r.get('toAmount')}  status={r.get('orderStatus')}")
except Exception as e:
    print(f"  Error: {e}")

# Spot trades — see if user sold ETH
print("\n=== RECENT SPOT TRADES (ETHUSDT, last 5) ===")
try:
    trades = c.get_my_trades(symbol="ETHUSDT", limit=5)
    if not trades:
        print("  (no ETHUSDT trades)")
    for t in trades[:5]:
        ts = t.get("time", 0) / 1000
        dt_str = dt.datetime.fromtimestamp(ts).isoformat()
        side = "BUY" if t.get("isBuyer") else "SELL"
        print(f"  [{dt_str}] {side} {t.get('qty')} ETH at {t.get('price')}")
except Exception as e:
    print(f"  Error: {e}")

# Spot Earn flexible balance
print("\n=== SIMPLE EARN FLEXIBLE ===")
try:
    res = c.get_simple_earn_flexible_product_position()
    rows = res.get("rows", []) if isinstance(res, dict) else []
    if not rows:
        print("  (none)")
    for r in rows[:5]:
        print(f"  {r.get('asset')}: {r.get('totalAmount')} (productId {r.get('productId')})")
except Exception as e:
    print(f"  Error: {e}")

# Auto-invest plans
print("\n=== ALL ASSETS via wallet/getuserAsset (any non-zero) ===")
try:
    assets = c.get_user_asset(needBtcValuation=True)
    for a in assets[:20]:
        free = float(a.get("free", 0))
        locked = float(a.get("locked", 0))
        if free + locked > 0:
            print(f"  {a.get('asset'):8s} free={free} locked={locked} btcValuation={a.get('btcValuation', 0)}")
except Exception as e:
    print(f"  Error: {e}")
