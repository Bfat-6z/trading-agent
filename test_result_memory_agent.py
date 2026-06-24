"""Turn test/exam failures into auditable learning memory.

This agent never mutates risk or execution. It only reads latest learning/test
artifacts and writes lessons, curriculum, and episode rows for downstream
consumers such as self_model and dashboard.
"""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from typing import Any, Iterable

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, read_json, write_json_atomic
from episodic_task_ledger import record_episode
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
LATEST_PATH = MEMORY_DIR / "test_result_memory_latest.json"
HISTORY_PATH = MEMORY_DIR / "test_result_memory_history.jsonl"
HEARTBEAT_PATH = STATE_DIR / "test_result_memory_agent_heartbeat.json"
PID_FILE = STATE_DIR / "test_result_memory_agent.pid"
STOP_FILE = STATE_DIR / "STOP_TEST_RESULT_MEMORY_AGENT"

SOURCE_FILES = {
    "daily_exam": MEMORY_DIR / "daily_exam_latest.json",
    "counterfactual": MEMORY_DIR / "counterfactual_latest.json",
    "shadow": MEMORY_DIR / "shadow_performance_latest.json",
    "walk_forward": MEMORY_DIR / "walk_forward_latest.json",
    "promotion": MEMORY_DIR / "promotion_board_latest.json",
    "learning_benchmark": MEMORY_DIR / "learning_exam_benchmark_latest.json",
}

def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default

def lesson_id(source: str, code: str) -> str:
    return f"lesson_{source}_{code}"

def threshold_breach(source: str, value: float, threshold: float, direction: str) -> bool:
    if direction == "lt":
        return value < threshold
    return value > threshold

def load_sources() -> dict[str, dict[str, Any]]:
    return {name: read_json(path, default={}) for name, path in SOURCE_FILES.items()}

def build_test_memory_lessons(sources: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    lessons: list[dict[str, Any]] = []
    daily_exam = sources["daily_exam"]
    counterfactual = sources["counterfactual"]
    shadow = sources["shadow"]
    walk_forward = sources["walk_forward"]
    promotion = sources["promotion"]
    benchmark = sources["learning_benchmark"]

    exam_quality = safe_float(daily_exam.get("quality_score"))
    cf_coverage = safe_float(counterfactual.get("coverage_pct"))
    shadow_fresh = shadow.get("fresh_window") if isinstance(shadow.get("fresh_window"), dict) else {}
    shadow_overall = (shadow_fresh.get("overall") if isinstance(shadow_fresh.get("overall"), dict) else None) or shadow.get("overall") or {}
    shadow_expectancy = safe_float(shadow_overall.get("expectancy"))
    shadow_profit_factor = safe_float(shadow_overall.get("profit_factor"))
    wf_status = str(walk_forward.get("status") or "missing")
    wf_running = int(walk_forward.get("running") or (walk_forward.get("by_status") or {}).get("running") or 0)
    wf_failed = int(walk_forward.get("failed") or (walk_forward.get("by_status") or {}).get("failed") or 0)
    promo_state = str(promotion.get("state") or "paper_learning")
    benchmark_score = safe_float(benchmark.get("score"))

    if threshold_breach("daily_exam", exam_quality, 70.0, "lt"):
        lessons.append({
            "lesson_id": lesson_id("daily_exam", "low_quality"),
            "source": "daily_exam",
            "severity": "high",
            "gap": "daily_exam_quality_low",
            "value": round(exam_quality, 2),
            "threshold": 70.0,
            "lesson": "Daily exam is not yet strong enough to count as improvement proof.",
            "next_action": "Collect more objective evidence before raising learning confidence.",
        })
    if threshold_breach("counterfactual", cf_coverage, 0.8, "lt"):
        lessons.append({
            "lesson_id": lesson_id("counterfactual", "low_coverage"),
            "source": "counterfactual",
            "severity": "high",
            "gap": "counterfactual_coverage_low",
            "value": round(cf_coverage, 4),
            "threshold": 0.8,
            "lesson": "Counterfactual replay coverage is still too low to trust setup tuning.",
            "next_action": "Replay more blocked and closed signals before any skill promotion.",
        })
    if threshold_breach("shadow", shadow_expectancy, 0.0, "lt") or threshold_breach("shadow", shadow_profit_factor, 1.0, "lt"):
        lessons.append({
            "lesson_id": lesson_id("shadow", "weak_edge"),
            "source": "shadow",
            "severity": "high",
            "gap": "shadow_edge_weak",
            "value": {"expectancy": round(shadow_expectancy, 8), "profit_factor": round(shadow_profit_factor, 4)},
            "threshold": {"expectancy": 0.0, "profit_factor": 1.0},
            "lesson": "Fresh shadow edge is still weak, so paper allocation should remain conservative.",
            "next_action": "Investigate setups, symbols, and regime conflicts before increasing paper risk.",
        })
    if wf_status in {"running", "failed"} or wf_running > 0 or wf_failed > 0:
        lessons.append({
            "lesson_id": lesson_id("walk_forward", wf_status),
            "source": "walk_forward",
            "severity": "medium",
            "gap": "walk_forward_not_done",
            "value": {"status": wf_status, "running": wf_running, "failed": wf_failed},
            "threshold": {"running": 0, "failed": 0, "status": "passed"},
            "lesson": "Walk-forward validation is not settled yet, so promotion must stay blocked.",
            "next_action": "Wait for future-window evidence before trusting new patches.",
        })
    if promo_state != "live_review_candidate" or bool(promotion.get("passed")) is False:
        lessons.append({
            "lesson_id": lesson_id("promotion", "blocked"),
            "source": "promotion",
            "severity": "medium",
            "gap": "promotion_blocked",
            "value": promo_state,
            "threshold": "live_review_candidate",
            "lesson": "Promotion remains blocked until objective gates pass.",
            "next_action": "Keep paper-only learning and avoid any live permission drift.",
        })
    if benchmark and benchmark_score < 1.0:
        for row in benchmark.get("lessons") if isinstance(benchmark.get("lessons"), list) else []:
            lessons.append({
                "lesson_id": row.get("scenario_id") or lesson_id("benchmark", row.get("name") or "unknown"),
                "source": "learning_benchmark",
                "severity": "medium",
                "gap": "scenario_mismatch",
                "value": {"scenario": row.get("name"), "expected_action": row.get("expected_action"), "actual_action": row.get("actual_action")},
                "threshold": "pass",
                "lesson": row.get("lesson") or "Benchmark scenario failed.",
                "next_action": row.get("next_action") or "Review scenario gate.",
            })
    return lessons

def build_payload(sources: dict[str, dict[str, Any]], lessons: list[dict[str, Any]]) -> dict[str, Any]:
    high_count = sum(1 for row in lessons if row.get("severity") == "high")
    medium_count = sum(1 for row in lessons if row.get("severity") == "medium")
    curriculum = [
        {"priority": "high" if row.get("severity") == "high" else "medium", "task": row.get("lesson"), "action": row.get("next_action"), "source": row.get("source"), "lesson_id": row.get("lesson_id")}
        for row in lessons
    ]
    episode_rows = []
    for row in lessons:
        episode_rows.append(
            record_episode(
                trigger="daily_exam" if row.get("source") == "daily_exam" else "manual",
                goal=row.get("lesson") or "test-to-memory lesson",
                decision={"source": row.get("source"), "lesson_id": row.get("lesson_id")},
                actions=[{"type": "curriculum_item", "priority": row.get("severity"), "task": row.get("lesson")}],
                outcome={"status": "lesson_created", "threshold": row.get("threshold"), "value": row.get("value")},
                lesson=row.get("lesson") or "",
                next_action=row.get("next_action") or "",
                context_refs=[str(row.get("lesson_id") or "")],
                quality=0.2 if row.get("severity") == "high" else 0.4,
                episode_id=str(row.get("lesson_id") or ""),
            )
        )
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "source_count": len(sources),
        "lesson_count": len(lessons),
        "high_severity_count": high_count,
        "medium_severity_count": medium_count,
        "lessons": lessons,
        "curriculum": curriculum[:20],
        "episode_snapshots": episode_rows[-10:],
        "known_gaps": sorted({row["gap"] for row in lessons if row.get("gap")}),
        "can_place_live_orders": False,
        "can_loosen_risk": False,
    }
    return payload

def write_heartbeat(status: str, payload: dict[str, Any] | None = None) -> None:
    write_json_atomic(HEARTBEAT_PATH, {"schema_version": SCHEMA_VERSION, "ts": utc_now(), "pid": os.getpid(), "status": status, **(payload or {})})

def run_once(output_path: Path = LATEST_PATH, history_path: Path = HISTORY_PATH) -> dict[str, Any]:
    sources = load_sources()
    lessons = build_test_memory_lessons(sources)
    payload = build_payload(sources, lessons)
    write_json_atomic(output_path, payload)
    append_jsonl(history_path, payload)
    write_heartbeat("ok", {"lesson_count": payload["lesson_count"], "high_severity_count": payload["high_severity_count"]})
    return payload

def interruptible_sleep(seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline and not STOP_FILE.exists():
        write_heartbeat("waiting", {"next_run_seconds": round(max(0.0, deadline - time.time()), 1)})
        time.sleep(min(30.0, max(0.0, deadline - time.time())))

def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Convert learning/test results into memory curriculum")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-seconds", type=float, default=1800.0)
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
        print(f"test_result_memory_agent lessons={result.get('lesson_count')} high={result.get('high_severity_count')}", flush=True)
        if args.once:
            break
        interruptible_sleep(args.interval_seconds)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
