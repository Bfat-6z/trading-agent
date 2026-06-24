"""Pre-place SL + TP reduceOnly orders BEFORE entry fills.
reduceOnly means orders only fire if position exists - safe to pre-place.
Fixes the 15s gap risk in monitor-based SL placement.
"""
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
from decimal import Decimal
c = spot_client()

POSITIONS = {
    "HYPEUSDT": {"sl": 58.30, "tp": 60.50, "qty": "1.53"},
    "EDENUSDT": {"sl": 0.0950, "tp": 0.1020, "qty": "414"},
}

info = c.futures_exchange_info()
for sym, cfg in POSITIONS.items():
    sym_info = next(s for s in info["symbols"] if s["symbol"]==sym)
    tick = Decimal(next(f["tickSize"] for f in sym_info["filters"] if f["filterType"]=="PRICE_FILTER"))
    sl_q = Decimal(str(cfg["sl"])).quantize(tick)
    tp_q = Decimal(str(cfg["tp"])).quantize(tick)

    print(f"\n=== {sym} ===")
    # Place SL
    try:
        c.futures_create_order(symbol=sym, side="SELL", type="STOP_MARKET",
            stopPrice=format(sl_q, "f"), quantity=cfg["qty"],
            reduceOnly="true", workingType="MARK_PRICE")
        print(f"  [OK] SL pre-placed @ ${sl_q} reduceOnly")
    except Exception as e:
        print(f"  [FAIL] SL: {str(e)[:100]}")

    # Place TP
    try:
        c.futures_create_order(symbol=sym, side="SELL", type="TAKE_PROFIT_MARKET",
            stopPrice=format(tp_q, "f"), quantity=cfg["qty"],
            reduceOnly="true", workingType="MARK_PRICE")
        print(f"  [OK] TP pre-placed @ ${tp_q} reduceOnly")
    except Exception as e:
        print(f"  [FAIL] TP: {str(e)[:100]}")

# Verify
print(f"\n=== VERIFY ===")
for sym in POSITIONS:
    algos = c._request_futures_api("get", "openAlgoOrders", True, data={"symbol": sym})
    print(f"\n{sym}:")
    for a in algos:
        ro = "reduceOnly" if a.get("reduceOnly") else "ENTRY"
        print(f"  {a['orderType']} {a['side']} qty {a['quantity']} trigger ${a['triggerPrice']} {ro}")
