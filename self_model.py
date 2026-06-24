"""Auditable self-model for the learning agent."""
from __future__ import annotations

import argparse
import os
import time
from pathlib import Path
from typing import Any, Iterable

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, read_json, read_jsonl, write_json_atomic
from event_store import safe_upsert_heartbeat
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
SELF_MODEL_LATEST = MEMORY_DIR / "self_model_latest.json"
PID_FILE = STATE_DIR / "self_model.pid"
HEARTBEAT_PATH = STATE_DIR / "self_model_heartbeat.json"
STOP_FILE = STATE_DIR / "STOP_SELF_MODEL"


def history_path() -> Path:
    return SELF_MODEL_LATEST.with_name("self_model_history.jsonl")


def count_jsonl(path: Path, limit: int = 5000) -> int:
    return len(read_jsonl(path, limit=limit))


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


def build_self_model() -> dict[str, Any]:
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

    review_queue = skill_review_queue(skills)
    curriculum: list[dict[str, Any]] = []
    if "no_post_trade_reviews_yet" in gaps:
        curriculum.append({"priority": "high", "task": "collect and review closed paper/shadow trades"})
    if "no_counterfactual_replays_yet" in gaps:
        curriculum.append({"priority": "high", "task": "run counterfactual replay on blocked and closed signals"})
    if "no_promoted_memories_yet" in gaps:
        curriculum.append({"priority": "medium", "task": "wait for repeated evidence before promoting durable memory"})
    if review_queue:
        curriculum.append({"priority": "medium", "task": "review stale or weak setup skills", "items": review_queue[:5]})

    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
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
        },
        "experience_counters": {
            "episodes": episodes_count,
            "post_trade_reviews": int(post_trade.get("review_count") or reviews_count),
            "counterfactual_replays": int(counterfactual.get("replay_count") or replay_count),
            "promoted_memories": promoted_count,
            "skill_review_items": len(review_queue),
        },
        "known_gaps": gaps,
        "skill_review_queue": review_queue,
        "self_questions": [
            "Which setup is failing because market regime changed?",
            "Which losses were good process versus bad process?",
            "Which blocked trades would have won in counterfactual replay?",
            "Which skill should be versioned, degraded, or retired?",
        ],
        "curriculum": curriculum,
        "can_trade_live": False,
        "can_loosen_risk": False,
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
