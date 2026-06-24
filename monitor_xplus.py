"""Monitor XPLUSUSDT long and tighten reduce-only SL on profit milestones.

This script never opens or adds to a position. It only maintains existing
protective exits for the active long and raises the stop when price advances.
"""
from __future__ import annotations

import os
import sys
import time
from decimal import Decimal

from dotenv import load_dotenv

from tradingagents.binance.client import spot_client


load_dotenv()

SYM = "XPLUSDT"
SIDE = "SELL"  # close long
INTERVAL_SECONDS = int(os.getenv("XPLUS_MONITOR_INTERVAL", "300"))

INITIAL_SL = Decimal("0.09223")
TAKE_PROFIT = Decimal("0.10224")

# mark trigger, new SL, label
MILESTONES = [
    (Decimal("0.09630"), Decimal("0.09355"), "BE+"),
    (Decimal("0.09850"), Decimal("0.09550"), "LOCK_1P5"),
    (Decimal("0.10050"), Decimal("0.09850"), "LOCK_3P7"),
]

c = spot_client()


def now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


def log(msg: str) -> None:
    print(f"[{now()}] {msg}", flush=True)


def as_price(value: Decimal, tick: Decimal) -> str:
    return format(value.quantize(tick), "f")


def as_qty(value: Decimal) -> str:
    return format(value.normalize(), "f")


def get_tick() -> Decimal:
    info = c.futures_exchange_info()
    sym_info = next(s for s in info["symbols"] if s["symbol"] == SYM)
    return Decimal(next(f["tickSize"] for f in sym_info["filters"] if f["filterType"] == "PRICE_FILTER"))


TICK = get_tick()


def get_position() -> dict | None:
    positions = c.futures_position_information(symbol=SYM)
    for pos in positions:
        if pos.get("symbol") != SYM:
            continue
        amount = Decimal(pos["positionAmt"])
        if amount != 0:
            return pos
    return None


def open_algos() -> list[dict]:
    return c._request_futures_api("get", "openAlgoOrders", True, data={"symbol": SYM})


def stop_orders(algos: list[dict]) -> list[dict]:
    return [
        a
        for a in algos
        if a.get("side") == SIDE
        and a.get("orderType") == "STOP_MARKET"
        and bool(a.get("reduceOnly"))
    ]


def tp_orders(algos: list[dict]) -> list[dict]:
    return [
        a
        for a in algos
        if a.get("side") == SIDE
        and a.get("orderType") == "TAKE_PROFIT_MARKET"
        and bool(a.get("reduceOnly"))
    ]


def highest_stop(algos: list[dict]) -> Decimal | None:
    stops = stop_orders(algos)
    if not stops:
        return None
    return max(Decimal(str(a["triggerPrice"])) for a in stops)


def place_sl(price: Decimal, qty: Decimal, label: str) -> bool:
    stop_price = as_price(price, TICK)
    try:
        c.futures_create_order(
            symbol=SYM,
            side=SIDE,
            type="STOP_MARKET",
            stopPrice=stop_price,
            quantity=as_qty(qty),
            reduceOnly="true",
            workingType="MARK_PRICE",
        )
        log(f"SL_PLACED {label} stop={stop_price} qty={as_qty(qty)}")
        return True
    except Exception as exc:
        log(f"SL_PLACE_FAIL {label} stop={stop_price} err={str(exc)[:160]}")
        return False


def place_tp(price: Decimal, qty: Decimal) -> bool:
    tp_price = as_price(price, TICK)
    try:
        c.futures_create_order(
            symbol=SYM,
            side=SIDE,
            type="TAKE_PROFIT_MARKET",
            stopPrice=tp_price,
            quantity=as_qty(qty),
            reduceOnly="true",
            workingType="MARK_PRICE",
        )
        log(f"TP_PLACED tp={tp_price} qty={as_qty(qty)}")
        return True
    except Exception as exc:
        log(f"TP_PLACE_FAIL tp={tp_price} err={str(exc)[:160]}")
        return False


def cancel_algo(algo_id: int | str) -> bool:
    try:
        c._request_futures_api("delete", "algo/order", True, data={"algoId": algo_id})
        log(f"ALGO_CANCELLED algoId={algo_id}")
        return True
    except Exception as exc:
        log(f"ALGO_CANCEL_FAIL algoId={algo_id} err={str(exc)[:140]}")
        return False


def cleanup_lower_stops(keep_stop: Decimal) -> None:
    try:
        algos = open_algos()
    except Exception as exc:
        log(f"ALGO_FETCH_FAIL cleanup err={str(exc)[:140]}")
        return

    for order in stop_orders(algos):
        trigger = Decimal(str(order["triggerPrice"]))
        if trigger < keep_stop:
            cancel_algo(order["algoId"])


def cancel_all_algos() -> None:
    try:
        algos = open_algos()
    except Exception as exc:
        log(f"ALGO_FETCH_FAIL final_cleanup err={str(exc)[:140]}")
        return
    for order in algos:
        if order.get("side") == SIDE and bool(order.get("reduceOnly")):
            cancel_algo(order["algoId"])


def ensure_base_protection(qty: Decimal) -> None:
    try:
        algos = open_algos()
    except Exception as exc:
        log(f"ALGO_FETCH_FAIL protection err={str(exc)[:140]}")
        return

    if not stop_orders(algos):
        place_sl(INITIAL_SL, qty, "INITIAL_REPAIR")
    if not tp_orders(algos):
        place_tp(TAKE_PROFIT, qty)


def realized_summary() -> None:
    try:
        trades = c.futures_account_trades(symbol=SYM, limit=10)
    except Exception as exc:
        log(f"TRADE_FETCH_FAIL err={str(exc)[:140]}")
        return
    realized = [Decimal(str(t.get("realizedPnl", "0"))) for t in trades]
    total = sum(realized, Decimal("0"))
    log(f"POSITION_CLOSED recent_realized_sum={total:+f}")


def current_stage(current_stop: Decimal | None) -> int:
    if current_stop is None:
        return -1
    stage = -1
    for idx, (_, sl_price, _) in enumerate(MILESTONES):
        if current_stop >= sl_price:
            stage = idx
    return stage


def main() -> int:
    log(f"MONITOR_START symbol={SYM} interval={INTERVAL_SECONDS}s tick={TICK}")

    while True:
        try:
            pos = get_position()
            if pos is None:
                log("NO_POSITION detected; cleaning reduce-only SELL algos then exit")
                realized_summary()
                cancel_all_algos()
                return 0

            amount = Decimal(pos["positionAmt"])
            if amount <= 0:
                log(f"UNEXPECTED_POSITION amount={amount}; refusing to manage non-long position")
                return 2

            qty = abs(amount)
            entry = Decimal(str(pos["entryPrice"]))
            mark = Decimal(str(pos["markPrice"]))
            pnl = Decimal(str(pos["unRealizedProfit"]))

            ensure_base_protection(qty)

            algos = open_algos()
            active_stop = highest_stop(algos)
            stage = current_stage(active_stop)
            active_stop_s = "NONE" if active_stop is None else format(active_stop, "f")
            log(f"STATUS entry={entry} mark={mark} uPnL={pnl:+f} qty={as_qty(qty)} activeSL={active_stop_s} stage={stage}")

            for idx, (trigger_mark, sl_price, label) in enumerate(MILESTONES):
                if idx <= stage:
                    continue
                if mark >= trigger_mark:
                    if place_sl(sl_price, qty, label):
                        cleanup_lower_stops(sl_price)
                    break

            time.sleep(INTERVAL_SECONDS)
        except KeyboardInterrupt:
            log("MONITOR_STOPPED_BY_USER")
            return 130
        except Exception as exc:
            log(f"ERR {str(exc)[:180]}")
            time.sleep(min(60, INTERVAL_SECONDS))


if __name__ == "__main__":
    sys.exit(main())
