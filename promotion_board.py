"""Promotion readiness board for paper -> shadow/live-review gates."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import read_json, read_jsonl, write_json_atomic
from market_learner import valid_paper_close
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
PROMOTION_LATEST = MEMORY_DIR / "promotion_board_latest.json"
PAPER_TRADES_PATH = MEMORY_DIR / "paper_trades.jsonl"
SKILL_PATCHES_APPLIED = MEMORY_DIR / "skill_patches_applied.jsonl"
WALK_FORWARD_LATEST = MEMORY_DIR / "walk_forward_latest.json"


REQUIREMENTS = {
    "paper_trades": 300,
    "shadow_closes": 1000,
    "lifecycle_completeness": 0.99,
    "daily_exam_avg": 80,
    "trial_days": 14,
}


def parse_ts(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def validated_paper_closes_since_reset(account: dict[str, Any], path: Path = PAPER_TRADES_PATH) -> int:
    reset_ts = parse_ts(account.get("created_at"))
    seen: set[tuple[str, str]] = set()
    count = 0
    for row in read_jsonl(path):
        if not valid_paper_close(row):
            continue
        close_ts_raw = str(row.get("close_ts") or row.get("ts") or "")
        close_ts = parse_ts(close_ts_raw)
        if reset_ts and (not close_ts or close_ts < reset_ts):
            continue
        trade_id = str(row.get("trade_id") or row.get("paper_trade_id") or row.get("close_id") or "")
        key = (trade_id, close_ts_raw)
        if key in seen:
            continue
        seen.add(key)
        count += 1
    return count


def walk_forward_metrics(walk_forward: dict[str, Any], active_patch_ids: list[str]) -> dict[str, Any]:
    by_status = walk_forward.get("by_status") if isinstance(walk_forward.get("by_status"), dict) else {}
    rows = walk_forward.get("rows") if isinstance(walk_forward.get("rows"), list) else []
    latest_by_patch = {str(row.get("patch_id")): row for row in rows if row.get("patch_id")}
    experiment_count = int(walk_forward.get("experiment_count") or 0)
    running = int(by_status.get("running") or 0)
    failed = int(by_status.get("failed") or 0)
    passed = int(by_status.get("passed") or 0)
    active_patch_count = len(active_patch_ids)
    missing_patch_ids = [patch_id for patch_id in active_patch_ids if patch_id not in latest_by_patch]
    running_patch_ids = [patch_id for patch_id in active_patch_ids if latest_by_patch.get(patch_id, {}).get("status") == "running"]
    failed_patch_ids = [patch_id for patch_id in active_patch_ids if latest_by_patch.get(patch_id, {}).get("status") == "failed"]
    passed_patch_ids = [patch_id for patch_id in active_patch_ids if latest_by_patch.get(patch_id, {}).get("status") == "passed"]
    required = active_patch_count > 0
    if not required:
        status = "not_required"
    elif missing_patch_ids:
        status = "missing"
    elif failed_patch_ids:
        status = "failed"
    elif running_patch_ids:
        status = "running"
    elif len(passed_patch_ids) == active_patch_count:
        status = "passed"
    else:
        status = "unknown"
    return {
        "walk_forward_required": required,
        "walk_forward_status": status,
        "walk_forward_experiments": experiment_count,
        "walk_forward_running": running,
        "walk_forward_failed": failed,
        "walk_forward_passed": passed,
        "active_skill_patches": active_patch_count,
        "active_skill_patch_ids": active_patch_ids,
        "walk_forward_missing_patch_ids": missing_patch_ids,
        "walk_forward_running_patch_ids": running_patch_ids,
        "walk_forward_failed_patch_ids": failed_patch_ids,
        "walk_forward_passed_patch_ids": passed_patch_ids,
    }


def evaluate_promotion(metrics: dict[str, Any], output_path: Path = PROMOTION_LATEST) -> dict[str, Any]:
    failures = []
    if int(metrics.get("paper_trades") or 0) < REQUIREMENTS["paper_trades"]:
        failures.append("insufficient_paper_trades")
    if int(metrics.get("shadow_closes") or 0) < REQUIREMENTS["shadow_closes"]:
        failures.append("insufficient_shadow_closes")
    if float(metrics.get("lifecycle_completeness") or 0.0) < REQUIREMENTS["lifecycle_completeness"]:
        failures.append("lifecycle_completeness_below_99pct")
    if float(metrics.get("daily_exam_avg") or 0.0) < REQUIREMENTS["daily_exam_avg"]:
        failures.append("daily_exam_below_threshold")
    if int(metrics.get("trial_days") or 0) < REQUIREMENTS["trial_days"]:
        failures.append("trial_too_short")
    if metrics.get("critical_dont_do_violation"):
        failures.append("critical_dont_do_violation")
    if metrics.get("portfolio_risk_status") == "critical":
        failures.append("portfolio_risk_critical")
    if metrics.get("walk_forward_required"):
        if metrics.get("walk_forward_status") == "missing":
            failures.append("walk_forward_missing")
        if metrics.get("walk_forward_failed_patch_ids") or metrics.get("walk_forward_status") == "failed":
            failures.append("walk_forward_validation_failed")
        if metrics.get("walk_forward_running_patch_ids") or metrics.get("walk_forward_status") == "running":
            failures.append("walk_forward_validation_running")
        if len(metrics.get("walk_forward_passed_patch_ids") or []) < int(metrics.get("active_skill_patches") or 0):
            failures.append("walk_forward_not_all_patches_passed")
    state = "live_review_candidate" if not failures else "paper_learning"
    payload = {"schema_version": SCHEMA_VERSION, "evaluated_at": utc_now(), "state": state, "passed": not failures, "failures": failures, "requirements": REQUIREMENTS, "metrics": metrics, "can_place_live_orders": False}
    write_json_atomic(output_path, payload)
    return payload


def evaluate_from_state(output_path: Path = PROMOTION_LATEST) -> dict[str, Any]:
    lifecycle = read_json(MEMORY_DIR / "trade_lifecycle_latest.json", default={})
    shadow = read_json(MEMORY_DIR / "shadow_performance_latest.json", default={})
    exam = read_json(MEMORY_DIR / "daily_exam_latest.json", default={})
    portfolio = read_json(MEMORY_DIR / "portfolio_risk_latest.json", default={})
    paper = read_json(STATE_DIR / "paper_account.json", default={})
    walk_forward = read_json(WALK_FORWARD_LATEST, default={})
    active_patch_ids = [
        str(row.get("patch_id"))
        for row in read_jsonl(SKILL_PATCHES_APPLIED)
        if row.get("status") == "paper_only_applied" and row.get("patch_id")
    ]
    account_paper_trades = int(paper.get("closed_trades") or paper.get("trades") or 0)
    validated_paper_trades = validated_paper_closes_since_reset(paper)
    paper_trades = min(account_paper_trades, validated_paper_trades) if account_paper_trades else validated_paper_trades
    metrics = {
        "paper_trades": paper_trades,
        "account_paper_trades": account_paper_trades,
        "validated_paper_trades": validated_paper_trades,
        "shadow_closes": int((shadow.get("overall") or {}).get("closed") or shadow.get("closed") or 0),
        "lifecycle_completeness": float(lifecycle.get("trade_lifecycle_completeness") or 0.0),
        "daily_exam_avg": float(exam.get("quality_score") or exam.get("exam_score") or 0.0),
        "trial_days": int(paper.get("trial_days") or 0),
        "portfolio_risk_status": portfolio.get("status"),
        **walk_forward_metrics(walk_forward, active_patch_ids),
    }
    return evaluate_promotion(metrics, output_path)
