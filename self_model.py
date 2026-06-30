"""Auditable self-model for the learning agent."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any, Iterable

import agent_work_queue as work_queue
from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, read_json, read_jsonl, write_json_atomic
from event_store import safe_upsert_heartbeat
from timebase import seconds_between, utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
SELF_MODEL_LATEST = MEMORY_DIR / "self_model_latest.json"
CURRICULUM_HISTORY = MEMORY_DIR / "self_model_curriculum_history.jsonl"
WORK_QUEUE_DB = STATE_DIR / "agent_jobs.sqlite"
MAX_CURRICULUM_TASKS_PER_RUN = 5
CURRICULUM_COOLDOWN_SECONDS = 6 * 3600
MAX_SELF_GENERATED_RETRIES = 3
PID_FILE = STATE_DIR / "self_model.pid"
HEARTBEAT_PATH = STATE_DIR / "self_model_heartbeat.json"
STOP_FILE = STATE_DIR / "STOP_SELF_MODEL"


def history_path() -> Path:
    return SELF_MODEL_LATEST.with_name("self_model_history.jsonl")


def count_jsonl(path: Path, limit: int = 5000) -> int:
    return len(read_jsonl(path, limit=limit))

def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except Exception:
        return default

def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default

def stable_digest(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=True, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def skill_review_queue(skills: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    skill_map = skills.get("skills") if isinstance(skills.get("skills"), dict) else {}
    for setup_id, skill in skill_map.items():
        if not isinstance(skill, dict):
            continue
        stats = skill.get("stats") if isinstance(skill.get("stats"), dict) else {}
        trades = int(stats.get("trades") or 0)
        expectancy = float(stats.get("expectancy") or 0.0)
        patch_count = int(skill.get("paper_shadow_patch_count") or 0)
        if trades < 20:
            rows.append({"setup_id": setup_id, "priority": 3, "reason": "needs_more_samples", "trades": trades, "expectancy": expectancy, "paper_shadow_patch_count": patch_count})
        elif expectancy < 0:
            rows.append({"setup_id": setup_id, "priority": 1, "reason": "negative_expectancy_review", "trades": trades, "expectancy": expectancy, "paper_shadow_patch_count": patch_count})
        elif patch_count > 0:
            rows.append({"setup_id": setup_id, "priority": 2, "reason": "patched_skill_needs_retest", "trades": trades, "expectancy": expectancy, "paper_shadow_patch_count": patch_count})
    return sorted(rows, key=lambda row: (row["priority"], row["setup_id"]))[:20]

def curriculum_job_type(item: dict[str, Any]) -> str:
    text = " ".join(str(item.get(key) or "") for key in ("gap", "task", "action", "reason", "source")).lower()
    if "counterfactual" in text or "replay" in text:
        return "replay_batch"
    if "post_trade" in text or "closed paper" in text or "review closed" in text:
        return "post_trade_review"
    if "skill_patch" in text or "retirement" in text or "patch" in text:
        return "skill_patch_review"
    if "setup" in text or "skill" in text:
        return "setup_review"
    if "daily_exam" in text or "benchmark" in text or "scenario" in text:
        return "daily_exam_task"
    if "source" in text or "market" in text or "data" in text:
        return "market_scan"
    return "daily_exam_task"

def curriculum_priority(item: dict[str, Any]) -> int:
    raw = item.get("priority")
    if isinstance(raw, str):
        base = {"critical": 95, "high": 90, "medium": 65, "low": 40}.get(raw.lower(), 50)
    elif isinstance(raw, (int, float)):
        value = int(raw)
        base = 100 - value * 10 if 0 < value <= 5 else value
    else:
        base = 50
    base += min(20, safe_int(item.get("occurrences")) * 3)
    base += min(10, int(safe_float(item.get("priority_score"))))
    return max(1, min(99, base))

def curriculum_signature(item: dict[str, Any]) -> str:
    return "curriculum_" + stable_digest(
        {
            "gap": item.get("gap"),
            "task": item.get("task"),
            "action": item.get("action"),
            "setup_id": item.get("setup_id"),
            "source": item.get("source"),
            "reason": item.get("reason"),
        }
    )

def evidence_ids_from_item(item: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for key in ("evidence_ids", "memory_ids", "lesson_id", "gap", "setup_id"):
        value = item.get(key)
        if isinstance(value, list):
            ids.extend(str(row) for row in value if row)
        elif value:
            ids.append(str(value))
    for idx, row in enumerate(item.get("items") if isinstance(item.get("items"), list) else []):
        if not isinstance(row, dict):
            continue
        setup_id = row.get("setup_id")
        reason = row.get("reason")
        if setup_id:
            ids.append(f"setup:{setup_id}")
        if reason:
            ids.append(f"skill_review:{reason}:{setup_id or idx}")
    if not ids:
        ids.append(curriculum_signature(item))
    return sorted(set(ids))

def refreshed_history_rows(history_rows: list[dict[str, Any]], db_path: Path) -> list[dict[str, Any]]:
    if not db_path.exists():
        return history_rows
    refreshed: list[dict[str, Any]] = []
    try:
        with work_queue.connect(db_path) as conn:
            for row in history_rows:
                task_id = row.get("curriculum_task_id")
                if not task_id:
                    refreshed.append(row)
                    continue
                found = conn.execute("SELECT status, completed_at, error FROM jobs WHERE job_id=?", (str(task_id),)).fetchone()
                if found:
                    status, completed_at, error = found
                    refreshed.append({**row, "status": status, "completed_at": completed_at, "error": error})
                else:
                    refreshed.append(row)
    except Exception:
        return history_rows
    return refreshed

def active_queue_signatures(db_path: Path) -> set[str]:
    if not db_path.exists():
        return set()
    signatures: set[str] = set()
    try:
        with work_queue.connect(db_path) as conn:
            rows = conn.execute("SELECT payload_json FROM jobs WHERE status IN ('queued','running')").fetchall()
        for (payload_json,) in rows:
            try:
                payload = json.loads(payload_json)
            except Exception:
                continue
            signature = payload.get("curriculum_signature") if isinstance(payload, dict) else None
            if signature:
                signatures.add(str(signature))
    except Exception:
        return set()
    return signatures

def is_recent_history(row: dict[str, Any], now_ts: str, cooldown_seconds: int) -> bool:
    delta = seconds_between(row.get("queued_at") or row.get("ts"), now_ts)
    return delta is not None and 0 <= delta < cooldown_seconds

def anti_loop_decision(item: dict[str, Any], history_rows: list[dict[str, Any]], now_ts: str, active_signatures: set[str] | None = None) -> dict[str, Any]:
    signature = curriculum_signature(item)
    same_rows = [row for row in history_rows if row.get("curriculum_signature") == signature]
    recent_rows = [row for row in same_rows if is_recent_history(row, now_ts, CURRICULUM_COOLDOWN_SECONDS)]
    self_failures = [
        row for row in same_rows
        if row.get("status") in {"failed", "throttled"} and row.get("source_partition") == "self_generated"
    ]
    if len(self_failures) >= MAX_SELF_GENERATED_RETRIES:
        return {"allowed": False, "reason": "self_generated_retry_circuit_breaker", "curriculum_signature": signature, "retry_count": len(same_rows)}
    if active_signatures and signature in active_signatures:
        return {"allowed": False, "reason": "curriculum_duplicate_active_job", "curriculum_signature": signature, "retry_count": len(same_rows)}
    if recent_rows:
        return {"allowed": False, "reason": "curriculum_cooldown_active", "curriculum_signature": signature, "retry_count": len(same_rows)}
    return {"allowed": True, "reason": "allowed", "curriculum_signature": signature, "retry_count": len(same_rows)}

def build_curriculum_tasks(curriculum: list[dict[str, Any]], history_rows: list[dict[str, Any]], now_ts: str, max_tasks: int = MAX_CURRICULUM_TASKS_PER_RUN, active_signatures: set[str] | None = None) -> dict[str, Any]:
    planned: list[dict[str, Any]] = []
    throttled: list[dict[str, Any]] = []
    seen_signatures: set[str] = set()
    normalized = [row for row in curriculum if isinstance(row, dict) and (row.get("task") or row.get("gap") or row.get("action"))]
    normalized.sort(key=lambda row: (-curriculum_priority(row), str(row.get("gap") or row.get("task") or "")))
    for item in normalized:
        decision = anti_loop_decision(item, history_rows, now_ts, active_signatures=active_signatures)
        signature = decision["curriculum_signature"]
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        task = {
            "schema_version": SCHEMA_VERSION,
            "curriculum_task_id": "self_model_task_" + stable_digest({"signature": signature}),
            "curriculum_signature": signature,
            "job_type": curriculum_job_type(item),
            "priority": curriculum_priority(item),
            "gap": item.get("gap"),
            "task": item.get("task") or item.get("gap"),
            "action": item.get("action"),
            "source": item.get("source") or "self_model",
            "source_partition": item.get("source_partition") or ("self_generated" if item.get("source") in {None, "self_model"} else "evidence_backed"),
            "evidence_ids": evidence_ids_from_item(item),
            "acceptance": item.get("acceptance_test") or item.get("acceptance") or "write latest/history output and cite evidence ids",
            "can_place_live_orders": False,
            "can_loosen_risk": False,
            "anti_loop": decision,
        }
        if not decision["allowed"]:
            throttled.append(task)
            continue
        if len(planned) >= max_tasks:
            task["anti_loop"] = {**decision, "allowed": False, "reason": "curriculum_task_budget_exhausted"}
            throttled.append(task)
            continue
        planned.append(task)
    return {"planned": planned, "throttled": throttled}

def enqueue_curriculum_tasks(tasks: list[dict[str, Any]], db_path: Path = WORK_QUEUE_DB, history_path_arg: Path = CURRICULUM_HISTORY) -> dict[str, Any]:
    queued: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    ts = utc_now()
    for task in tasks:
        payload = {
            "curriculum_task_id": task["curriculum_task_id"],
            "curriculum_signature": task["curriculum_signature"],
            "gap": task.get("gap"),
            "task": task.get("task"),
            "action": task.get("action"),
            "source": task.get("source"),
            "source_partition": task.get("source_partition"),
            "evidence_ids": task.get("evidence_ids") or [],
            "acceptance": task.get("acceptance"),
            "can_place_live_orders": False,
            "can_loosen_risk": False,
        }
        result = work_queue.enqueue_job(task["job_type"], payload, priority=task["priority"], job_id=task["curriculum_task_id"], db_path=db_path)
        status = "queued" if result.get("ok") and result.get("inserted") else "duplicate_existing_job" if result.get("ok") else "enqueue_failed"
        row = {**task, "queued_at": ts, "status": status, "queue_result": result}
        append_jsonl(history_path_arg, row)
        if result.get("ok") and result.get("inserted"):
            queued.append(row)
        else:
            skipped.append(row)
    return {"queued_count": len(queued), "skipped_count": len(skipped), "queued": queued, "skipped": skipped}

def homework_score(planned: list[dict[str, Any]], throttled: list[dict[str, Any]], queued_report: dict[str, Any], history_rows: list[dict[str, Any]]) -> dict[str, Any]:
    recent_done = sum(1 for row in history_rows[-50:] if row.get("status") in {"done", "completed"})
    assigned = len(planned)
    queued = int(queued_report.get("queued_count") or 0)
    throttled_count = len(throttled)
    score = 0.0
    if assigned:
        score += min(0.5, queued / max(1, assigned) * 0.5)
    score += min(0.3, recent_done * 0.05)
    if throttled_count == 0:
        score += 0.2
    return {
        "assigned": assigned,
        "queued": queued,
        "completed_recent": recent_done,
        "throttled": throttled_count,
        "score": round(score, 4),
        "can_place_live_orders": False,
        "can_loosen_risk": False,
    }


def build_self_model() -> dict[str, Any]:
    now_ts = utc_now()
    account = read_json(STATE_DIR / "paper_account.json", default={})
    bias = read_json(MEMORY_DIR / "execution_bias.json", default={})
    paper_loop = read_json(MEMORY_DIR / "autonomous_paper_trading_loop_latest.json", default={})
    dream = read_json(MEMORY_DIR / "dream_cycle_latest.json", default={})
    skills = read_json(MEMORY_DIR / "setup_skills.json", default={})
    lifecycle = read_json(MEMORY_DIR / "trade_lifecycle_latest.json", default={})
    post_trade = read_json(MEMORY_DIR / "post_trade_learning_latest.json", default={})
    counterfactual = read_json(MEMORY_DIR / "counterfactual_latest.json", default={})
    memory = read_json(MEMORY_DIR / "memory_consolidation_latest.json", default={})
    self_improvement = read_json(MEMORY_DIR / "self_improvement_latest.json", default={})
    promotion = read_json(MEMORY_DIR / "promotion_board_latest.json", default={})
    test_memory = read_json(MEMORY_DIR / "test_result_memory_latest.json", default={})
    benchmark = read_json(MEMORY_DIR / "learning_exam_benchmark_latest.json", default={})
    sources = read_json(STATE_DIR / "data_sources_latest.json", default={})

    episodes_count = count_jsonl(MEMORY_DIR / "episodes.jsonl")
    reviews_count = count_jsonl(MEMORY_DIR / "post_trade_reviews.jsonl")
    replay_count = count_jsonl(MEMORY_DIR / "counterfactual_replays.jsonl")
    promoted_count = int(memory.get("promoted_count") or count_jsonl(MEMORY_DIR / "memory_promoted.jsonl"))

    gaps: list[str] = []
    if not lifecycle.get("learning_allowed", False):
        gaps.append("trade_lifecycle_not_clean")
    if int(post_trade.get("review_count") or reviews_count) == 0:
        gaps.append("no_post_trade_reviews_yet")
    if int(counterfactual.get("replay_count") or replay_count) == 0:
        gaps.append("no_counterfactual_replays_yet")
    if promoted_count == 0:
        gaps.append("no_promoted_memories_yet")
    if not sources.get("sources"):
        gaps.append("source_registry_not_initialized")
    for gap in test_memory.get("known_gaps") if isinstance(test_memory.get("known_gaps"), list) else []:
        if gap and gap not in gaps:
            gaps.append(str(gap))

    review_queue = skill_review_queue(skills)
    curriculum: list[dict[str, Any]] = []
    if "no_post_trade_reviews_yet" in gaps:
        curriculum.append({"priority": "high", "gap": "no_post_trade_reviews_yet", "task": "collect and review closed paper/shadow trades", "source": "self_model", "evidence_ids": ["gap:no_post_trade_reviews_yet"]})
    if "no_counterfactual_replays_yet" in gaps:
        curriculum.append({"priority": "high", "gap": "no_counterfactual_replays_yet", "task": "run counterfactual replay on blocked and closed signals", "source": "self_model", "evidence_ids": ["gap:no_counterfactual_replays_yet"]})
    if "no_promoted_memories_yet" in gaps:
        curriculum.append({"priority": "medium", "gap": "no_promoted_memories_yet", "task": "wait for repeated evidence before promoting durable memory", "source": "self_model", "evidence_ids": ["gap:no_promoted_memories_yet"]})
    if review_queue:
        curriculum.append({"priority": "medium", "gap": "setup_skill_review_due", "task": "review stale or weak setup skills", "source": "self_model", "items": review_queue[:5]})
    for item in test_memory.get("curriculum") if isinstance(test_memory.get("curriculum"), list) else []:
        if isinstance(item, dict):
            curriculum.append({**item, "source": item.get("source") or "test_result_memory"})
    for item in test_memory.get("priority_curriculum") if isinstance(test_memory.get("priority_curriculum"), list) else []:
        if isinstance(item, dict):
            curriculum.append(
                {
                    "priority": item.get("priority") or "medium",
                    "task": item.get("task") or item.get("gap"),
                    "action": item.get("action"),
                    "source": item.get("source") or "test_result_memory_priority",
                    "gap": item.get("gap"),
                    "priority_score": item.get("priority_score"),
                    "occurrences": item.get("occurrences"),
                }
            )

    priority_queue = test_memory.get("priority_curriculum") if isinstance(test_memory.get("priority_curriculum"), list) else []
    top_priority = priority_queue[0] if priority_queue and isinstance(priority_queue[0], dict) else {}
    curriculum_history_path = MEMORY_DIR / "self_model_curriculum_history.jsonl"
    queue_db_path = STATE_DIR / "agent_jobs.sqlite"
    curriculum_history = refreshed_history_rows(read_jsonl(curriculum_history_path, limit=500), queue_db_path)
    task_plan = build_curriculum_tasks(curriculum, curriculum_history, now_ts, active_signatures=active_queue_signatures(queue_db_path))
    queue_report = enqueue_curriculum_tasks(task_plan["planned"], db_path=queue_db_path, history_path_arg=curriculum_history_path)
    homework = homework_score(task_plan["planned"], task_plan["throttled"], queue_report, curriculum_history)

    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": now_ts,
        "identity": {
            "agent_name": "paper_learning_trading_agent",
            "purpose": "learn market behavior through paper/shadow evidence before any live review",
            "allowed_actions": ["observe", "paper_trade", "shadow_evaluate", "write_memory", "propose_skill_patch", "propose_code_change"],
            "forbidden_actions": ["place_live_order", "loosen_risk_without_gate", "self_apply_source_code"],
        },
        "mode": promotion.get("state") or "paper_learning",
        "current_state": {
            "paper_equity": account.get("equity"),
            "paper_closed_trades": account.get("closed_trades") or account.get("trades") or 0,
            "risk_posture": bias.get("risk_posture"),
            "min_signal_score": bias.get("min_signal_score"),
            "sleep_until": bias.get("sleep_until"),
            "last_paper_action": (paper_loop.get("decision") or {}).get("action") or paper_loop.get("action"),
            "dream_high_risk_count": (dream.get("bias_patch") or {}).get("high_risk_count"),
            "self_improvement_readiness": self_improvement.get("readiness"),
            "learning_benchmark_score": benchmark.get("score"),
            "test_memory_lessons": test_memory.get("lesson_count"),
            "test_memory_top_gap": top_priority.get("gap"),
            "test_memory_top_priority_score": top_priority.get("priority_score"),
        },
        "experience_counters": {
            "episodes": episodes_count,
            "post_trade_reviews": int(post_trade.get("review_count") or reviews_count),
            "counterfactual_replays": int(counterfactual.get("replay_count") or replay_count),
            "promoted_memories": promoted_count,
            "skill_review_items": len(review_queue),
            "test_result_lessons": int(test_memory.get("lesson_count") or 0),
            "learning_benchmark_scenarios": int(benchmark.get("scenario_count") or 0),
            "test_memory_priority_items": len(priority_queue),
        },
        "known_gaps": gaps,
        "skill_review_queue": review_queue,
        "test_memory_priority_queue": priority_queue[:10],
        "self_questions": [
            "Which setup is failing because market regime changed?",
            "Which losses were good process versus bad process?",
            "Which blocked trades would have won in counterfactual replay?",
            "Which skill should be versioned, degraded, or retired?",
        ],
        "curriculum": curriculum,
        "curriculum_tasks": task_plan["planned"],
        "curriculum_throttled": task_plan["throttled"],
        "work_queue": {
            "db_path": str(queue_db_path),
            "queued_count": queue_report.get("queued_count"),
            "skipped_count": queue_report.get("skipped_count"),
            "queued_job_ids": [row.get("curriculum_task_id") for row in queue_report.get("queued", [])],
        },
        "homework_score": homework,
        "can_trade_live": False,
        "can_loosen_risk": False,
        "can_place_live_orders": False,
    }
    write_json_atomic(SELF_MODEL_LATEST, payload)
    append_jsonl(history_path(), payload)
    return payload


def write_heartbeat(status: str, payload: dict[str, Any] | None = None) -> None:
    row = {"schema_version": SCHEMA_VERSION, "ts": utc_now(), "pid": os.getpid(), "status": status, **(payload or {})}
    write_json_atomic(HEARTBEAT_PATH, row)
    safe_upsert_heartbeat("self_model", status, row, ts=row["ts"])


def interruptible_sleep(seconds: float) -> None:
    deadline = time.time() + seconds
    while time.time() < deadline and not STOP_FILE.exists():
        write_heartbeat("waiting", {"next_run_seconds": round(max(0.0, deadline - time.time()), 1)})
        time.sleep(min(30.0, max(0.0, deadline - time.time())))


def run_loop(args: argparse.Namespace) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)
    PID_FILE.write_text(str(os.getpid()), encoding="ascii")
    if STOP_FILE.exists():
        STOP_FILE.unlink()
    while not STOP_FILE.exists():
        try:
            model = build_self_model()
            write_heartbeat("ok", {"mode": model.get("mode"), "known_gaps": len(model.get("known_gaps") or []), "skill_review_items": len(model.get("skill_review_queue") or [])})
            print(f"self_model mode={model.get('mode')} gaps={len(model.get('known_gaps') or [])} skill_reviews={len(model.get('skill_review_queue') or [])}", flush=True)
        except Exception as exc:
            write_heartbeat("error", {"error": str(exc)[:300]})
            print(f"self_model_error {str(exc)[:160]}", flush=True)
        if args.once:
            break
        interruptible_sleep(args.interval_minutes * 60)
    return 0


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run auditable self-model snapshots for the paper-learning agent")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval-minutes", type=float, default=10.0)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.interval_minutes <= 0:
        parser.error("--interval-minutes must be positive")
    return args


def main(argv: Iterable[str] | None = None) -> int:
    return run_loop(parse_args(argv))


if __name__ == "__main__":
    raise SystemExit(main())
