"""Cancel current breakout orders, replace with 2x size for vol up."""
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
from decimal import Decimal
import time

c = spot_client()

# New sizing - 2x previous
configs = [
    {
        "sym": "HYPEUSDT",
        "trigger": 58.90,
        "sl": 58.30,
        "tp": 60.50,
        "margin": 3.0,
        "lev": 30,
    },
    {
        "sym": "EDENUSDT",
        "trigger": 0.0967,
        "sl": 0.0950,
        "tp": 0.1020,
        "margin": 2.0,
        "lev": 20,
    },
]

info = c.futures_exchange_info()

for cfg in configs:
    sym = cfg["sym"]
    print(f"\n=== {sym} resize ===")
    # Cancel existing
    try:
        algos = c._request_futures_api("get", "openAlgoOrders", True, data={"symbol": sym})
        for a in algos:
            if a["side"] == "BUY" and not a.get("reduceOnly"):
                print(f"  Found old entry algo {a['algoId']} - need cancel via UI (delete endpoint broken)")
        # Try regular cancel-all (for non-algo orders)
        c.futures_cancel_all_open_orders(symbol=sym)
        print(f"  Cancelled regular orders for {sym}")
    except Exception as e:
        print(f"  Cancel err: {str(e)[:100]}")
    time.sleep(0.5)

# Note: algo cancellation via API broken; must use Binance UI or different endpoint
# Try DELETE via batch endpoint
for cfg in configs:
    sym = cfg["sym"]
    try:
        algos = c._request_futures_api("get", "openAlgoOrders", True, data={"symbol": sym})
        algo_ids = [a["algoId"] for a in algos if a["side"]=="BUY" and not a.get("reduceOnly")]
        if algo_ids:
            print(f"\n{sym} - trying batch cancel algo IDs: {algo_ids}")
            for aid in algo_ids:
                try:
                    # Try multiple endpoint variants
                    r = c._request_futures_api("delete", "algo/order", True, data={"algoId": str(aid)})
                    print(f"  Cancel {aid}: {r}")
                except Exception as e:
                    print(f"  Cancel {aid} fail: {str(e)[:80]}")
    except Exception as e:
        print(f"  {sym}: {e}")

time.sleep(1)

# Place new bigger orders
print(f"\n=== Placing new bigger orders ===")
for cfg in configs:
    sym = cfg["sym"]
    sym_info = next(s for s in info["symbols"] if s["symbol"]==sym)
    tick = Decimal(next(f["tickSize"] for f in sym_info["filters"] if f["filterType"]=="PRICE_FILTER"))
    step = Decimal(next(f["stepSize"] for f in sym_info["filters"] if f["filterType"]=="LOT_SIZE"))

    notional = cfg["margin"] * cfg["lev"]
    qty = (Decimal(str(notional)) / Decimal(str(cfg["trigger"]))).quantize(step)
    trigger_q = Decimal(str(cfg["trigger"])).quantize(tick)

    print(f"\n{sym}:")
    print(f"  Margin ${cfg['margin']} × {cfg['lev']}x = ${notional} notional")
    print(f"  Qty: {qty}  Trigger: ${trigger_q}")
    sl_loss = float(qty) * (cfg["trigger"] - cfg["sl"])
    tp_gain = float(qty) * (cfg["tp"] - cfg["trigger"])
    print(f"  Max loss: ${sl_loss:.3f}  Max gain: ${tp_gain:.3f}  R:R {tp_gain/sl_loss:.2f}")

    try:
        c.futures_change_leverage(symbol=sym, leverage=cfg["lev"])
        o = c.futures_create_order(
            symbol=sym, side="BUY", type="STOP_MARKET",
            stopPrice=format(trigger_q, "f"),
            quantity=str(qty),
            workingType="MARK_PRICE",
            timeInForce="GTC"
        )
        print(f"  [OK] New STOP_MARKET BUY placed")
    except Exception as e:
        print(f"  [FAIL] {str(e)[:100]}")
