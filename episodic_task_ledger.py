"""Hermes-style episodic task ledger for the paper-learning agent."""
from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
from typing import Any, Iterable

from agent_data_contracts import SCHEMA_VERSION, validate_contract
from atomic_state import append_jsonl_once, read_jsonl, write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
EPISODES_JSONL = MEMORY_DIR / "episodes.jsonl"
EPISODES_LATEST = MEMORY_DIR / "episodes_latest.json"

ALLOWED_TRIGGERS = {
    "paper_open",
    "paper_close",
    "shadow_close",
    "daily_exam",
    "llm_reasoning",
    "market_event",
    "news_event",
    "skill_patch",
    "memory_consolidation",
    "preflight",
    "manual",
}


def stable_episode_id(trigger: str, goal: str, context_refs: list[str] | None = None, seed: str | None = None) -> str:
    raw = {
        "trigger": trigger,
        "goal": goal,
        "context_refs": context_refs or [],
        "seed": seed or "",
    }
    digest = hashlib.sha256(str(raw).encode("utf-8")).hexdigest()[:20]
    return f"episode_{digest}"


def clamp_quality(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except Exception:
        return 0.0


def build_episode(
    trigger: str,
    goal: str,
    decision: dict[str, Any] | None = None,
    actions: list[dict[str, Any]] | None = None,
    outcome: dict[str, Any] | None = None,
    lesson: str = "",
    next_action: str = "",
    context_refs: list[str] | None = None,
    quality: float = 0.0,
    episode_id: str | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    refs = [str(item) for item in (context_refs or []) if item]
    row = {
        "schema_version": SCHEMA_VERSION,
        "episode_id": episode_id or stable_episode_id(trigger, goal, refs, seed=error),
        "ts": utc_now(),
        "trigger": str(trigger or "manual"),
        "goal": str(goal or "unspecified"),
        "context_refs": refs,
        "decision": decision or {},
        "actions": actions or [],
        "outcome": outcome or {},
        "lesson": str(lesson or ""),
        "next_action": str(next_action or ""),
        "quality": clamp_quality(quality),
    }
    if error:
        row["error"] = str(error)[:500]
        row["outcome"] = {**row["outcome"], "status": "error"}
    return row


def validate_episode(row: dict[str, Any]) -> tuple[bool, list[str], list[str]]:
    contract = validate_contract("episode", row)
    errors = list(contract.errors)
    warnings = list(contract.warnings)
    if row.get("trigger") not in ALLOWED_TRIGGERS:
        warnings.append("unknown_trigger")
    if not isinstance(row.get("actions"), list):
        errors.append("actions_not_list")
    if not isinstance(row.get("decision"), dict):
        errors.append("decision_not_dict")
    if not isinstance(row.get("outcome"), dict):
        errors.append("outcome_not_dict")
    return not errors, errors, warnings


def append_episode(row: dict[str, Any], path: Path = EPISODES_JSONL, latest_path: Path = EPISODES_LATEST) -> dict[str, Any]:
    ok, errors, warnings = validate_episode(row)
    if not ok:
        raise ValueError(f"episode validation failed: {errors}")
    inserted = append_jsonl_once(path, row, "episode_id")
    latest = summarize_episodes(path)
    latest.update({"last_episode": row, "last_inserted": inserted, "last_warnings": warnings, "updated_at": utc_now()})
    write_json_atomic(latest_path, latest)
    return latest


def record_episode(
    trigger: str,
    goal: str,
    decision: dict[str, Any] | None = None,
    actions: list[dict[str, Any]] | None = None,
    outcome: dict[str, Any] | None = None,
    lesson: str = "",
    next_action: str = "",
    context_refs: list[str] | None = None,
    quality: float = 0.0,
    episode_id: str | None = None,
    path: Path = EPISODES_JSONL,
    latest_path: Path = EPISODES_LATEST,
) -> dict[str, Any]:
    return append_episode(
        build_episode(
            trigger=trigger,
            goal=goal,
            decision=decision,
            actions=actions,
            outcome=outcome,
            lesson=lesson,
            next_action=next_action,
            context_refs=context_refs,
            quality=quality,
            episode_id=episode_id,
        ),
        path,
        latest_path,
    )


def summarize_episodes(path: Path = EPISODES_JSONL, limit: int = 5000) -> dict[str, Any]:
    rows = read_jsonl(path, limit=limit)
    by_trigger: dict[str, int] = {}
    quality_sum = 0.0
    linked_refs = 0
    errors = 0
    for row in rows:
        trigger = str(row.get("trigger") or "unknown")
        by_trigger[trigger] = by_trigger.get(trigger, 0) + 1
        quality_sum += clamp_quality(row.get("quality"))
        if row.get("context_refs"):
            linked_refs += 1
        if row.get("error") or (isinstance(row.get("outcome"), dict) and row["outcome"].get("status") == "error"):
            errors += 1
    return {
        "schema_version": SCHEMA_VERSION,
        "episode_count": len(rows),
        "by_trigger": by_trigger,
        "avg_quality": round(quality_sum / len(rows), 4) if rows else 0.0,
        "linked_episode_count": linked_refs,
        "error_episode_count": errors,
    }


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Inspect or append episodic task ledger rows")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--trigger", default="manual")
    parser.add_argument("--goal", default="manual ledger note")
    parser.add_argument("--lesson", default="")
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    if args.summary:
        summary = summarize_episodes()
        write_json_atomic(EPISODES_LATEST, {**summary, "updated_at": utc_now()})
        print(summary)
        return 0
    latest = record_episode(args.trigger, args.goal, lesson=args.lesson, quality=0.0)
    print(latest)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
