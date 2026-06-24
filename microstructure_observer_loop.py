"""Local microstructure observer loop for derivatives/orderbook/liquidations.

This daemon reads local source snapshots produced by adapters and evaluates them
into deterministic paper-trading context. It intentionally does not call any
exchange or social API itself.
"""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from typing import Any, Iterable

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, read_json, write_json_atomic
from derivatives_observer import evaluate_derivatives
from liquidation_observer import aggregate_liquidations
from orderbook_observer import evaluate_orderbook
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
PID_FILE = STATE_DIR / "microstructure_observer_loop.pid"
HEARTBEAT_PATH = STATE_DIR / "microstructure_observer_loop_heartbeat.json"
STOP_FILE = STATE_DIR / "STOP_MICROSTRUCTURE_OBSERVER_LOOP"
LATEST_PATH = MEMORY_DIR / "microstructure_observer_loop_latest.json"
HISTORY_PATH = MEMORY_DIR / "microstructure_observer_loop_history.jsonl"
DERIVATIVES_SOURCE = STATE_DIR / "derivatives_latest.json"
ORDERBOOK_SOURCE = STATE_DIR / "orderbook_microstructure_latest.json"
LIQUIDATIONS_SOURCE = STATE_DIR / "liquidations_latest.json"

def has_any(payload: dict[str, Any], keys: Iterable[str]) -> bool:
    return any(key in payload and payload.get(key) is not None for key in keys)

def write_heartbeat(status: str, payload: dict[str, Any] | None = None) -> None:
    write_json_atomic(HEARTBEAT_PATH, {"schema_version": SCHEMA_VERSION, "ts": utc_now(), "pid": os.getpid(), "status": status, **(payload or {})})

def write_latest(payload: dict[str, Any]) -> dict[str, Any]:
    row = {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), **payload, "can_place_live_orders": False}
    write_json_atomic(LATEST_PATH, row)
    append_jsonl(HISTORY_PATH, row)
    return row

def run_once() -> dict[str, Any]:
    derivatives_source = read_json(DERIVATIVES_SOURCE, default={})
    orderbook_source = read_json(ORDERBOOK_SOURCE, default={})
    liquidations_source = read_json(LIQUIDATIONS_SOURCE, default={})
    results: dict[str, Any] = {}
    if isinstance(derivatives_source, dict) and derivatives_source.get("symbol") and has_any(derivatives_source, ["oi_now", "oi_prev"]):
        results["derivatives"] = evaluate_derivatives(
            derivatives_source.get("symbol"),
            derivatives_source.get("funding_rate"),
            derivatives_source.get("oi_now"),
            derivatives_source.get("oi_prev"),
            derivatives_source.get("long_short_ratio"),
            derivatives_source.get("taker_buy_sell_ratio"),
        )
    elif isinstance(derivatives_source, dict) and derivatives_source.get("symbol"):
        results["derivatives"] = derivatives_source
    if isinstance(orderbook_source, dict) and orderbook_source.get("symbol") and ("bids" in orderbook_source or "asks" in orderbook_source):
        results["orderbook"] = evaluate_orderbook(orderbook_source.get("symbol"), orderbook_source.get("bids") or [], orderbook_source.get("asks") or [], float(orderbook_source.get("max_spread_bps") or 8.0))
    elif isinstance(orderbook_source, dict) and orderbook_source.get("symbol"):
        results["orderbook"] = orderbook_source
    if isinstance(liquidations_source, dict) and liquidations_source.get("symbol") and "events" in liquidations_source:
        events = liquidations_source.get("events") if isinstance(liquidations_source.get("events"), list) else []
        results["liquidations"] = aggregate_liquidations(liquidations_source.get("symbol"), events, float(liquidations_source.get("burst_threshold_notional") or 1_000_000.0))
    elif isinstance(liquidations_source, dict) and liquidations_source.get("symbol"):
        results["liquidations"] = liquidations_source
    status = "ok" if results else "waiting_for_sources"
    row = write_latest({"status": status, "result_count": len(results), "results": results})
    write_heartbeat(status, {"result_count": len(results)})
    return row

def interruptible_sleep(seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline and not STOP_FILE.exists():
        write_heartbeat("waiting", {"next_run_seconds": round(max(0.0, deadline - time.time()), 1)})
        time.sleep(min(30.0, max(0.0, deadline - time.time())))

def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run local microstructure observer loop")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.interval_seconds <= 0:
        parser.error("--interval-seconds must be positive")
    return args

def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    if STOP_FILE.exists():
        STOP_FILE.unlink()
    while not STOP_FILE.exists():
        row = run_once()
        print(f"microstructure_observer_loop status={row.get('status')} results={row.get('result_count')}", flush=True)
        if args.once:
            break
        interruptible_sleep(args.interval_seconds)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
