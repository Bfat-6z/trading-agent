"""Scheduled paper-only trading brain loop.

The loop consumes prepared paper candidates from a safe local queue or a local
state file, runs deterministic gates, and writes heartbeat/latest/history. It
does not fetch live markets, place exchange orders, or enable live execution.
"""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from typing import Any, Iterable

from agent_data_contracts import SCHEMA_VERSION
from agent_work_queue import claim_next_of_types, complete_job, recover_stale_locks
from atomic_state import append_jsonl, read_json, write_json_atomic
from autonomous_paper_trading_brain import decide_paper_action
from circuit_breaker import evaluate_circuit_breakers
from kill_switch import kill_switch_active
from live_permission_firewall import evaluate_live_permission
from paper_portfolio_manager import load_account
from runtime_config import load_runtime_config
from setup_skill_library import load_library
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
PID_FILE = STATE_DIR / "autonomous_paper_trading_loop.pid"
HEARTBEAT_PATH = STATE_DIR / "autonomous_paper_trading_loop_heartbeat.json"
STOP_FILE = STATE_DIR / "STOP_AUTONOMOUS_PAPER_TRADING_LOOP"
LATEST_PATH = MEMORY_DIR / "autonomous_paper_trading_loop_latest.json"
HISTORY_PATH = MEMORY_DIR / "autonomous_paper_trading_loop_history.jsonl"
CANDIDATES_PATH = MEMORY_DIR / "paper_candidates_latest.json"
ALLOWED_QUEUE_TYPES = ["market_scan", "setup_review"]

def write_heartbeat(status: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
    row = {"schema_version": SCHEMA_VERSION, "ts": utc_now(), "pid": os.getpid(), "status": status, **(payload or {})}
    write_json_atomic(HEARTBEAT_PATH, row)
    return row

def write_latest(row: dict[str, Any]) -> dict[str, Any]:
    payload = {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), **row, "can_place_live_orders": False}
    write_json_atomic(LATEST_PATH, payload)
    append_jsonl(HISTORY_PATH, payload)
    return payload

def setup_stats_from_library() -> list[dict[str, Any]]:
    library = load_library()
    skills = library.get("skills") if isinstance(library.get("skills"), dict) else {}
    rows: list[dict[str, Any]] = []
    for setup_id, skill in skills.items():
        if not isinstance(skill, dict):
            continue
        stats = skill.get("stats") if isinstance(skill.get("stats"), dict) else {}
        rows.append({"setup_id": setup_id, **stats, "metadata": skill.get("metadata") if isinstance(skill.get("metadata"), dict) else {}})
    return rows

def normalize_candidates(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    if isinstance(payload.get("candidates"), list):
        return [row for row in payload["candidates"] if isinstance(row, dict)]
    candidate = payload.get("candidate")
    if isinstance(candidate, dict):
        return [candidate]
    if payload.get("symbol") and payload.get("side"):
        return [payload]
    return []

def load_file_candidate_batch(path: Path = CANDIDATES_PATH) -> dict[str, Any]:
    payload = read_json(path, default={})
    return payload if isinstance(payload, dict) else {}

def load_queue_candidate_batch(worker_id: str) -> tuple[dict[str, Any], str | None]:
    job = claim_next_of_types(worker_id, ALLOWED_QUEUE_TYPES)
    if not job:
        return {}, None
    payload = job.get("payload") if isinstance(job.get("payload"), dict) else {}
    candidates = normalize_candidates(payload)
    if not candidates:
        complete_job(str(job["job_id"]), ok=False, error="missing_paper_candidates")
        return {}, str(job["job_id"])
    return {**payload, "source": "queue", "job": job, "candidates": candidates}, str(job["job_id"])

def circuit_metrics(account: dict[str, Any]) -> dict[str, Any]:
    return {
        "daily_loss_pct": account.get("daily_loss_pct", 0),
        "losing_streak": account.get("losing_streak", 0),
        "max_slippage_bps": account.get("max_slippage_bps", 0),
        "source_status": account.get("source_status", "ok"),
    }

def run_once(worker_id: str = "autonomous_paper_trading_loop") -> dict[str, Any]:
    recover_stale_locks()
    if kill_switch_active():
        row = write_latest({"status": "blocked", "action": "skip", "reason": "kill_switch_active"})
        write_heartbeat("blocked", {"reason": "kill_switch_active"})
        return row
    live_gate = evaluate_live_permission({"action": "paper_decision", "mode": "paper"})
    if not live_gate.get("allowed"):
        row = write_latest({"status": "blocked", "action": "skip", "reason": "live_firewall_block", "live_gate": live_gate})
        write_heartbeat("blocked", {"reason": "live_firewall_block"})
        return row
    account = load_account()
    circuit = evaluate_circuit_breakers(circuit_metrics(account))
    if not circuit.get("allowed"):
        row = write_latest({"status": "blocked", "action": "skip", "reason": "circuit_breaker", "circuit": circuit})
        write_heartbeat("blocked", {"reason": "circuit_breaker"})
        return row
    batch, job_id = load_queue_candidate_batch(worker_id)
    if not batch:
        batch = {**load_file_candidate_batch(), "source": "file"}
    candidates = normalize_candidates(batch)
    setup_stats = batch.get("setup_stats") if isinstance(batch.get("setup_stats"), list) else setup_stats_from_library()
    config = load_runtime_config()
    exploration_allowed = bool(config.get("feature_flags", {}).get("paper_exploration")) or any(bool(row.get("exploration_allowed")) for row in candidates)
    decision = decide_paper_action(candidates, setup_stats, account, exploration_allowed=exploration_allowed)
    row = write_latest({"status": "ok", "source": batch.get("source"), "job_id": job_id, "candidate_count": len(candidates), "exploration_allowed": exploration_allowed, "decision": decision, "circuit": circuit})
    if job_id:
        complete_job(job_id, ok=True)
    write_heartbeat("ok", {"candidate_count": len(candidates), "last_action": decision.get("action")})
    return row

def interruptible_sleep(seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline and not STOP_FILE.exists():
        write_heartbeat("waiting", {"next_run_seconds": round(max(0.0, deadline - time.time()), 1)})
        time.sleep(min(30.0, max(0.0, deadline - time.time())))

def read_pid(path: Path = PID_FILE) -> int | None:
    try:
        return int(path.read_text(encoding="ascii").strip())
    except Exception:
        return None

def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run scheduled autonomous paper-only trading brain loop")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=60.0)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.interval_seconds <= 0:
        parser.error("--interval-seconds must be positive")
    return args

def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if args.status:
        print({"pid": read_pid(), "latest": str(LATEST_PATH), "heartbeat": str(HEARTBEAT_PATH), "stop_file": str(STOP_FILE)})
        return 0
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    if STOP_FILE.exists():
        STOP_FILE.unlink()
    while not STOP_FILE.exists():
        row = run_once()
        print(f"autonomous_paper_trading_loop status={row.get('status')} action={(row.get('decision') or {}).get('action', row.get('action'))}", flush=True)
        if args.once:
            break
        interruptible_sleep(args.interval_seconds)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
