"""Supervised loop for fresh shadow would-trade evaluation.

This daemon uses public market candles only. It never loads account keys and
never submits orders. Its job is to keep fresh shadow performance current so
skill/risk learning can use would-trade evidence without mixing old batches.
"""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from typing import Any, Callable, Iterable

import shadow_trade_evaluator as ev
from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
PID_FILE = STATE_DIR / "shadow_trade_evaluator_loop.pid"
HEARTBEAT_PATH = STATE_DIR / "shadow_trade_evaluator_loop_heartbeat.json"
STOP_FILE = STATE_DIR / "STOP_SHADOW_TRADE_EVALUATOR_LOOP"
LATEST_PATH = MEMORY_DIR / "shadow_trade_evaluator_loop_latest.json"
HISTORY_PATH = MEMORY_DIR / "shadow_trade_evaluator_loop_history.jsonl"

Fetcher = Callable[[str, int, int, str], list[dict]]


def write_heartbeat(status: str, payload: dict[str, Any] | None = None) -> None:
    write_json_atomic(
        HEARTBEAT_PATH,
        {
            "schema_version": SCHEMA_VERSION,
            "ts": utc_now(),
            "pid": os.getpid(),
            "status": status,
            **(payload or {}),
        },
    )


def write_latest(payload: dict[str, Any]) -> dict[str, Any]:
    row = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        **payload,
        "can_place_live_orders": False,
    }
    write_json_atomic(LATEST_PATH, row)
    append_jsonl(HISTORY_PATH, row)
    return row


def row_is_terminal(row: dict[str, Any]) -> bool:
    status = str(row.get("status") or "")
    reason = str(row.get("reason") or "")
    return status == "closed" or reason == "malformed"


def latest_close_map(rows: list[dict]) -> dict[str, dict]:
    latest: dict[str, dict] = {}
    for row in ev.latest_by_close_id(rows):
        close_id = row.get("close_id")
        if close_id:
            latest[str(close_id)] = row
    return latest


def filter_evaluable_shadows(shadows: list[dict], assumptions: ev.Assumptions, existing_rows: list[dict]) -> tuple[list[dict], dict[str, int]]:
    latest = latest_close_map(existing_rows)
    assumption_id = ev.assumption_hash(assumptions)
    selected: list[dict] = []
    stats = {"terminal_skipped": 0, "retryable_selected": 0, "missing_shadow_id": 0}
    for shadow in shadows:
        shadow_id = shadow.get("shadow_id")
        if not shadow_id:
            stats["missing_shadow_id"] += 1
            continue
        close_id = ev.close_id(str(shadow_id), assumption_id)
        previous = latest.get(close_id)
        if previous and row_is_terminal(previous):
            stats["terminal_skipped"] += 1
            continue
        if previous:
            stats["retryable_selected"] += 1
        selected.append(shadow)
    return selected, stats


def should_append_evaluation(row: dict, previous: dict | None) -> bool:
    if previous is None:
        return True
    if row_is_terminal(previous):
        return False
    if row_is_terminal(row):
        return True
    return (previous.get("status"), previous.get("reason")) != (row.get("status"), row.get("reason"))


def append_evaluations(rows: list[dict], output_path: Path = ev.SHADOW_CLOSE_JSONL) -> dict[str, int]:
    latest = latest_close_map(ev.read_jsonl(output_path))
    append_rows: list[dict] = []
    duplicate_rows = 0
    for row in rows:
        close_id = row.get("close_id")
        previous = latest.get(str(close_id)) if close_id else None
        if should_append_evaluation(row, previous):
            append_rows.append(row)
            if close_id:
                latest[str(close_id)] = row
        else:
            duplicate_rows += 1
    ev.append_jsonl(output_path, append_rows)
    for row in append_rows:
        ev.safe_append_event("shadow_trade_evaluator_loop", "shadow_close", row, ts=row.get("close_ts") or utc_now())
    return {"new_rows": len(append_rows), "duplicate_rows": duplicate_rows}


def default_fetcher(symbol: str, start_ms: int, end_ms: int, interval: str) -> list[dict]:
    return ev.fetch_klines(symbol, start_ms, end_ms, interval, use_cache=True)


def run_once(
    *,
    max_age_hours: float | None = 24.0,
    max_trades: int | None = 100,
    interval: str = "1m",
    fee_rate: str = "0.0005",
    slippage_bps: str = "2",
    max_hold_seconds: int = 180,
    rate_limit_cooldown_seconds: int = 900,
    fetcher: Fetcher | None = None,
) -> dict[str, Any]:
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    assumptions = ev.Assumptions(
        interval=interval,
        fee_rate=str(fee_rate),
        slippage_bps=str(slippage_bps),
        max_hold_seconds=int(max_hold_seconds),
    )
    shadows, read_stats = ev.read_shadow_opens([ev.SHADOW_JSONL, ev.SCALP_JSONL])
    if max_age_hours is not None:
        cutoff = int((time.time() - max_age_hours * 3600) * 1000)
        shadows = [row for row in shadows if (ev.ts_ms(row.get("ts")) or 0) >= cutoff]
    existing_rows = ev.read_jsonl(ev.SHADOW_CLOSE_JSONL)
    evaluable, filter_stats = filter_evaluable_shadows(shadows, assumptions, existing_rows)
    selected = evaluable[: max_trades or len(evaluable)]
    run_id = "shadow_eval_loop_" + ev.utc_stamp()
    rows = ev.evaluate_many(
        selected,
        assumptions,
        run_id,
        fetcher or default_fetcher,
        max_trades=None,
        backoff_path=ev.RATE_LIMIT_STATE_JSON,
        rate_limit_cooldown_seconds=rate_limit_cooldown_seconds,
    )
    append_stats = append_evaluations(rows, ev.SHADOW_CLOSE_JSONL)
    all_rows = ev.read_jsonl(ev.SHADOW_CLOSE_JSONL)
    performance = ev.write_performance_outputs(all_rows, run_id, ev.SHADOW_PERFORMANCE_JSON, ev.REPORTS_DIR)
    overall = performance.get("overall") if isinstance(performance.get("overall"), dict) else {}
    fresh = performance.get("fresh_window") if isinstance(performance.get("fresh_window"), dict) else {}
    fresh_quality = fresh.get("data_quality") if isinstance(fresh.get("data_quality"), dict) else {}
    backoff = ev.rate_limit_backoff(ev.RATE_LIMIT_STATE_JSON)
    status = "ok"
    if backoff.get("active"):
        status = "rate_limited_backoff"
    elif not selected:
        status = "waiting_for_shadow_candidates"
    payload = write_latest(
        {
            "status": status,
            "run_id": run_id,
            "read_stats": read_stats,
            "filter_stats": filter_stats,
            "candidate_count": len(shadows),
            "eligible_count": len(evaluable),
            "evaluated": len(rows),
            "new_rows": append_stats["new_rows"],
            "duplicate_rows": append_stats["duplicate_rows"],
            "assumption_hash": ev.assumption_hash(assumptions),
            "backoff": backoff,
            "performance": {
                "closed": overall.get("closed", 0),
                "win_rate": overall.get("win_rate", 0),
                "expectancy": overall.get("expectancy", 0),
                "profit_factor": overall.get("profit_factor", 0),
                "fresh_row_count": fresh.get("row_count", 0),
                "fresh_quality": fresh_quality,
            },
        }
    )
    write_heartbeat(status, {"evaluated": len(rows), "new_rows": append_stats["new_rows"], "eligible_count": len(evaluable)})
    return payload


def interruptible_sleep(seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline and not STOP_FILE.exists():
        write_heartbeat("waiting", {"next_run_seconds": round(max(0.0, deadline - time.time()), 1)})
        time.sleep(min(30.0, max(0.0, deadline - time.time())))


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run fresh shadow trade evaluator loop")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=600.0)
    parser.add_argument("--max-age-hours", type=float, default=24.0)
    parser.add_argument("--max-trades", type=int, default=100)
    parser.add_argument("--rate-limit-cooldown-seconds", type=int, default=900)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.interval_seconds <= 0:
        parser.error("--interval-seconds must be positive")
    if args.max_trades <= 0:
        parser.error("--max-trades must be positive")
    return args


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    if STOP_FILE.exists():
        STOP_FILE.unlink()
    while not STOP_FILE.exists():
        row = run_once(
            max_age_hours=args.max_age_hours,
            max_trades=args.max_trades,
            rate_limit_cooldown_seconds=args.rate_limit_cooldown_seconds,
        )
        print(
            f"shadow_trade_evaluator_loop status={row.get('status')} evaluated={row.get('evaluated')} new_rows={row.get('new_rows')}",
            flush=True,
        )
        if args.once:
            break
        interruptible_sleep(args.interval_seconds)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
