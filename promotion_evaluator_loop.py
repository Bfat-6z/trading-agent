"""Scheduled promotion readiness evaluator.

This keeps the promotion board current for the dashboard. It never grants live
order permission; `promotion_board` always returns can_place_live_orders=false.
"""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from typing import Any, Iterable

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, write_json_atomic
from promotion_board import evaluate_from_state
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
PID_FILE = STATE_DIR / "promotion_evaluator_loop.pid"
HEARTBEAT_PATH = STATE_DIR / "promotion_evaluator_loop_heartbeat.json"
STOP_FILE = STATE_DIR / "STOP_PROMOTION_EVALUATOR_LOOP"
LATEST_PATH = MEMORY_DIR / "promotion_evaluator_loop_latest.json"
HISTORY_PATH = MEMORY_DIR / "promotion_evaluator_loop_history.jsonl"

def write_heartbeat(status: str, payload: dict[str, Any] | None = None) -> None:
    write_json_atomic(HEARTBEAT_PATH, {"schema_version": SCHEMA_VERSION, "ts": utc_now(), "pid": os.getpid(), "status": status, **(payload or {})})

def run_once() -> dict[str, Any]:
    result = evaluate_from_state()
    payload = {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "status": "ok", "promotion": result, "can_place_live_orders": False}
    write_json_atomic(LATEST_PATH, payload)
    append_jsonl(HISTORY_PATH, payload)
    write_heartbeat("ok", {"state": result.get("state"), "passed": bool(result.get("passed"))})
    return payload

def interruptible_sleep(seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline and not STOP_FILE.exists():
        write_heartbeat("waiting", {"next_run_seconds": round(max(0.0, deadline - time.time()), 1)})
        time.sleep(min(30.0, max(0.0, deadline - time.time())))

def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run promotion readiness evaluator loop")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=300.0)
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
        result = run_once()
        promotion = result.get("promotion") or {}
        print(f"promotion_evaluator state={promotion.get('state')} passed={promotion.get('passed')}", flush=True)
        if args.once:
            break
        interruptible_sleep(args.interval_seconds)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
