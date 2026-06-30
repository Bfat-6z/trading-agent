"""Host runtime checks for local 24/7 paper learner."""
from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, Iterable

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import read_json, write_json_atomic
from timebase import seconds_between, utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
HOST_RUNTIME_LATEST = STATE_DIR / "host_runtime_latest.json"
AUTOSTART_PROOF_PATH = STATE_DIR / "autostart_proof.json"
PID_FILE = STATE_DIR / "host_runtime_monitor.pid"
HEARTBEAT_PATH = STATE_DIR / "host_runtime_monitor_heartbeat.json"
STOP_FILE = STATE_DIR / "host_runtime_monitor.stop"
REQUIRED_AUTOSTART_FIELDS = {
    "trigger",
    "working_dir",
    "venv_python",
    "user_context",
    "env_source",
    "run_whether_user_logged_on",
    "post_reboot_assertion",
    "verification_source",
    "task_query_ok",
    "verified_at",
}


def strict_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off", ""}:
                return False
    return False

def field_present(payload: dict[str, Any], field: str) -> bool:
    if field not in payload:
        return False
    value = payload.get(field)
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True

def validate_autostart_proof(proof: dict[str, Any]) -> dict[str, Any]:
    missing = sorted(field for field in REQUIRED_AUTOSTART_FIELDS if not field_present(proof, field))
    verification_source = proof.get("verification_source")
    verified_source_ok = verification_source in {"task_scheduler", "windows_service", "operator_signed"}
    run_logged_on = strict_bool(proof.get("run_whether_user_logged_on"))
    post_reboot = strict_bool(proof.get("post_reboot_assertion"))
    task_query_ok = strict_bool(proof.get("task_query_ok"))
    ok = not missing and run_logged_on and post_reboot and verified_source_ok and task_query_ok
    return {
        "ok": ok,
        "missing": missing,
        "trigger": proof.get("trigger"),
        "working_dir": proof.get("working_dir"),
        "venv_python": proof.get("venv_python"),
        "user_context": proof.get("user_context"),
        "env_source": proof.get("env_source"),
        "run_whether_user_logged_on": run_logged_on,
        "post_reboot_assertion": post_reboot,
        "verification_source": verification_source,
        "task_query_ok": task_query_ok,
        "verified_at": proof.get("verified_at"),
        "verification_source_ok": verified_source_ok,
    }


def sleep_resume_gap(previous_checked_at: str | None, current_checked_at: str, threshold_seconds: float = 900.0) -> dict[str, Any]:
    gap = seconds_between(previous_checked_at, current_checked_at) if previous_checked_at else None
    detected = gap is not None and gap > threshold_seconds
    return {
        "detected": detected,
        "gap_seconds": round(gap, 3) if gap is not None else None,
        "threshold_seconds": threshold_seconds,
        "pause_paper_opens": detected,
        "promotion_window_valid": not detected,
        "replay_required": detected,
    }

def sleep_resume_replay_pending(payload: dict[str, Any]) -> bool:
    sleep_resume = payload.get("sleep_resume") if isinstance(payload.get("sleep_resume"), dict) else {}
    if not sleep_resume:
        return False
    if sleep_resume.get("replay_acknowledged") or sleep_resume.get("replay_ack_at"):
        return False
    return bool(sleep_resume.get("replay_required") or sleep_resume.get("pause_paper_opens"))

def latch_sleep_resume_pause(gap: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    if not sleep_resume_replay_pending(previous):
        return gap
    prior = previous.get("sleep_resume") if isinstance(previous.get("sleep_resume"), dict) else {}
    latched = dict(gap)
    latched.update(
        {
            "detected": bool(gap.get("detected") or prior.get("detected")),
            "pause_paper_opens": True,
            "promotion_window_valid": False,
            "replay_required": True,
            "latched": True,
            "latched_from_checked_at": previous.get("checked_at"),
            "reason": "sleep_resume_replay_required",
        }
    )
    return latched


def paper_opens_paused_by_runtime(path: Path = HOST_RUNTIME_LATEST, max_age_seconds: float = 900.0) -> dict[str, Any]:
    payload = read_json(path, default={})
    sleep_resume = payload.get("sleep_resume") if isinstance(payload.get("sleep_resume"), dict) else {}
    age = seconds_between(payload.get("checked_at"), utc_now()) if payload.get("checked_at") else None
    fresh = age is not None and 0 <= age <= max_age_seconds
    missing = not path.exists() or not payload.get("checked_at")
    stale = not missing and not fresh
    replay_required = bool(sleep_resume.get("replay_required"))
    sleep_paused = bool(sleep_resume.get("pause_paper_opens") or replay_required)
    paused = bool(missing or stale or sleep_paused)
    if missing:
        reason = "host_runtime_missing"
    elif stale:
        reason = "host_runtime_stale"
    elif sleep_paused:
        reason = "sleep_resume_replay_required" if replay_required else "sleep_resume_gap_detected"
    else:
        reason = "ok"
    return {
        "paused": paused,
        "reason": reason,
        "checked_at": payload.get("checked_at"),
        "age_seconds": round(age, 3) if age is not None else None,
        "replay_required": replay_required,
        "promotion_window_valid": bool(sleep_resume.get("promotion_window_valid", True)) and not paused,
    }

def acknowledge_sleep_resume_replay(path: Path = HOST_RUNTIME_LATEST, *, actor: str = "counterfactual_replay_agent", detail: dict[str, Any] | None = None) -> dict[str, Any]:
    payload = read_json(path, default={})
    if not payload:
        return {"ok": False, "reason": "host_runtime_missing", "path": str(path)}
    sleep_resume = dict(payload.get("sleep_resume") if isinstance(payload.get("sleep_resume"), dict) else {})
    if not (sleep_resume.get("replay_required") or sleep_resume.get("pause_paper_opens")):
        return {"ok": True, "reason": "no_replay_pending", "path": str(path)}
    now = utc_now()
    sleep_resume.update(
        {
            "pause_paper_opens": False,
            "promotion_window_valid": True,
            "replay_required": False,
            "replay_acknowledged": True,
            "replay_ack_at": now,
            "replay_ack_by": actor,
        }
    )
    if detail:
        sleep_resume["replay_ack_detail"] = detail
    payload["sleep_resume"] = sleep_resume
    payload["updated_at"] = now
    write_json_atomic(path, payload)
    return {"ok": True, "reason": "replay_acknowledged", "path": str(path), "sleep_resume": sleep_resume}


def check_host_runtime(
    min_free_gb: float = 1.0,
    output_path: Path = HOST_RUNTIME_LATEST,
    *,
    autostart_proof_path: Path = AUTOSTART_PROOF_PATH,
    sleep_gap_threshold_seconds: float = 900.0,
) -> dict[str, Any]:
    usage = shutil.disk_usage(ROOT)
    free_gb = usage.free / (1024 ** 3)
    errors = []
    warnings = []
    checked_at = utc_now()
    previous = read_json(output_path, default={})
    gap = latch_sleep_resume_pause(sleep_resume_gap(previous.get("checked_at"), checked_at, sleep_gap_threshold_seconds), previous)
    autostart_proof = validate_autostart_proof(read_json(autostart_proof_path, default={}))
    if free_gb < min_free_gb:
        errors.append("low_disk_space")
    if not autostart_proof["ok"]:
        warnings.append("windows_autostart_not_confirmed")
    if gap["detected"]:
        warnings.append("sleep_resume_gap_detected")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "checked_at": checked_at,
        "status": "critical" if errors else "warn" if warnings else "ok",
        "free_disk_gb": round(free_gb, 3),
        "errors": errors,
        "warnings": warnings,
        "autostart_confirmed": autostart_proof["ok"],
        "autostart_proof": autostart_proof,
        "sleep_resume": gap,
    }
    write_json_atomic(output_path, payload)
    return payload

def write_heartbeat(payload: dict[str, Any], status: str = "running") -> dict[str, Any]:
    now = utc_now()
    heartbeat = {
        "schema_version": SCHEMA_VERSION,
        "agent": "host_runtime_monitor",
        "pid": os.getpid(),
        "ts": now,
        "updated_at": now,
        "status": status,
        "last_run": payload,
    }
    write_json_atomic(HEARTBEAT_PATH, heartbeat)
    return heartbeat

def run_once(min_free_gb: float = 1.0, sleep_gap_threshold_seconds: float = 900.0) -> dict[str, Any]:
    payload = check_host_runtime(min_free_gb=min_free_gb, sleep_gap_threshold_seconds=sleep_gap_threshold_seconds)
    write_heartbeat(payload, status=payload.get("status", "running"))
    return payload

def interruptible_sleep(seconds: float) -> bool:
    deadline = time.time() + max(0.0, seconds)
    while time.time() < deadline:
        if STOP_FILE.exists():
            return False
        time.sleep(min(1.0, max(0.0, deadline - time.time())))
    return not STOP_FILE.exists()

def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monitor host runtime health for paper-only learning loops")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=300.0)
    parser.add_argument("--min-free-gb", type=float, default=1.0)
    parser.add_argument("--sleep-gap-threshold-seconds", type=float, default=900.0)
    return parser.parse_args(list(argv) if argv is not None else None)

def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if args.status:
        print(json.dumps(read_json(HOST_RUNTIME_LATEST, default={"status": "missing"}), ensure_ascii=True, indent=2, sort_keys=True))
        return 0
    PID_FILE.parent.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    if args.once:
        print(json.dumps(run_once(min_free_gb=args.min_free_gb, sleep_gap_threshold_seconds=args.sleep_gap_threshold_seconds), ensure_ascii=True, indent=2, sort_keys=True))
        return 0
    while not STOP_FILE.exists():
        run_once(min_free_gb=args.min_free_gb, sleep_gap_threshold_seconds=args.sleep_gap_threshold_seconds)
        if not interruptible_sleep(args.interval_seconds):
            break
    write_heartbeat(read_json(HOST_RUNTIME_LATEST, default={}), status="stopped")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
