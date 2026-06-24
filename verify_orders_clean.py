"""Verify only 1 entry order per symbol remains."""
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
c = spot_client()

for sym in ['HYPEUSDT', 'EDENUSDT']:
    algos = c._request_futures_api('get', 'openAlgoOrders', True, data={'symbol': sym})
    buy_entries = [a for a in algos if a["side"]=="BUY" and not a.get("reduceOnly")]
    print(f"\n=== {sym} ===")
    print(f"Open BUY entry orders: {len(buy_entries)}")
    for a in buy_entries:
        print(f"  algoId {a['algoId']} qty {a['quantity']} trigger ${a['triggerPrice']}")
    if len(buy_entries) == 1:
        print(f"  ✓ CLEAN - only new order remains")
    elif len(buy_entries) == 2:
        print(f"  ✗ DUPLICATE still present - cancel via UI needed")
    else:
        print(f"  ? UNEXPECTED count")
