"""Promotion readiness board for paper -> shadow/live-review gates."""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import read_json, read_jsonl, write_json_atomic
from market_learner import valid_paper_close
from timebase import utc_now
from trade_lifecycle_validator import TRADE_LIFECYCLE_QUARANTINE, active_quarantined_trade_ids

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
PROMOTION_LATEST = MEMORY_DIR / "promotion_board_latest.json"
PAPER_TRADES_PATH = MEMORY_DIR / "paper_trades.jsonl"
SKILL_PATCHES_APPLIED = MEMORY_DIR / "skill_patches_applied.jsonl"
WALK_FORWARD_LATEST = MEMORY_DIR / "walk_forward_latest.json"
REAL_SCORING_LATEST = MEMORY_DIR / "real_scoring_board_latest.json"
DAILY_EXAM_HISTORY = MEMORY_DIR / "daily_exam_history.jsonl"
REAL_SCORING_STALE_SECONDS = 6 * 60 * 60


REQUIREMENTS = {
    "paper_trades": 300,
    "shadow_closes": 1000,
    "lifecycle_completeness": 0.99,
    # daily_exam_avg removed as a HARD GATE: it is an AI self-exam (diagnostic
    # only, echo-chamber signal). Per the meta-loop mandate, diagnostics must not
    # gate promotion. Real promotion evidence = holdout + DSR, not self-scoring.
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


def validated_paper_closes_since_reset(account: dict[str, Any], path: Path = PAPER_TRADES_PATH, quarantine_path: Path = TRADE_LIFECYCLE_QUARANTINE) -> int:
    reset_ts = parse_ts(account.get("created_at"))
    quarantined_trade_ids = active_quarantined_trade_ids(read_jsonl(quarantine_path))
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
        if trade_id in quarantined_trade_ids:
            continue
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
    inconclusive = int(by_status.get("inconclusive") or 0)
    active_patch_count = len(active_patch_ids)
    missing_patch_ids = [patch_id for patch_id in active_patch_ids if patch_id not in latest_by_patch]
    running_patch_ids = [patch_id for patch_id in active_patch_ids if latest_by_patch.get(patch_id, {}).get("status") == "running"]
    failed_patch_ids = [patch_id for patch_id in active_patch_ids if latest_by_patch.get(patch_id, {}).get("status") == "failed"]
    inconclusive_patch_ids = [patch_id for patch_id in active_patch_ids if latest_by_patch.get(patch_id, {}).get("status") == "inconclusive"]
    passed_patch_ids = [patch_id for patch_id in active_patch_ids if latest_by_patch.get(patch_id, {}).get("status") == "passed"]
    stale_sla_seconds = int(walk_forward.get("stale_sla_seconds") or 24 * 60 * 60)
    updated_at = parse_ts(walk_forward.get("updated_at"))
    stale = True
    if updated_at:
        stale = (datetime.now(timezone.utc) - updated_at).total_seconds() > stale_sla_seconds
    required_manifest_fields = ("metric_manifest_digest", "cited_metric_manifest_digest", "code_config_digest", "candidate_policy_digest", "frozen_partition_digest")
    digest_mismatch_patch_ids = []
    for patch_id in active_patch_ids:
        row = latest_by_patch.get(patch_id)
        if not row:
            continue
        missing_manifest = any(not row.get(field) for field in required_manifest_fields)
        digest_mismatch = bool(row.get("metric_manifest_digest") and row.get("cited_metric_manifest_digest") and row.get("metric_manifest_digest") != row.get("cited_metric_manifest_digest"))
        spec = row.get("walk_forward_window_spec") if isinstance(row.get("walk_forward_window_spec"), dict) else {}
        if not spec.get("window_id") or not row.get("family_correction") or missing_manifest or digest_mismatch:
            digest_mismatch_patch_ids.append(patch_id)
    required = active_patch_count > 0
    if not required:
        status = "not_required"
    elif missing_patch_ids:
        status = "missing"
    elif failed_patch_ids:
        status = "failed"
    elif inconclusive_patch_ids:
        status = "inconclusive"
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
        "walk_forward_inconclusive": inconclusive,
        "walk_forward_passed": passed,
        "active_skill_patches": active_patch_count,
        "active_skill_patch_ids": active_patch_ids,
        "walk_forward_missing_patch_ids": missing_patch_ids,
        "walk_forward_running_patch_ids": running_patch_ids,
        "walk_forward_failed_patch_ids": failed_patch_ids,
        "walk_forward_inconclusive_patch_ids": inconclusive_patch_ids,
        "walk_forward_passed_patch_ids": passed_patch_ids,
        "walk_forward_stale": stale,
        "walk_forward_updated_at": walk_forward.get("updated_at"),
        "walk_forward_stale_sla_seconds": stale_sla_seconds,
        "walk_forward_digest_mismatch_patch_ids": digest_mismatch_patch_ids,
    }

def real_scoring_state_metrics(scoring: dict[str, Any], account: dict[str, Any], validated_paper_trades: int) -> dict[str, Any]:
    if not scoring:
        return {
            "real_scoring_missing": True,
            "real_scoring_passed": None,
            "real_scoring_snapshot_id": None,
            "real_scoring_metric_manifest_digest": None,
            "real_scoring_stale": True,
            "real_scoring_before_account_reset": False,
            "real_scoring_trade_count_mismatch": False,
            "real_scoring_missing_watermark": True,
        }
    now = datetime.now(timezone.utc)
    as_of = parse_ts(scoring.get("as_of"))
    report_cutoff = parse_ts(scoring.get("report_cutoff"))
    reset_ts = parse_ts(account.get("created_at"))
    scored_trades = int((scoring.get("overall") or {}).get("trades") or 0)
    age_seconds = (now - as_of).total_seconds() if as_of else None
    stale = True if age_seconds is None else age_seconds > REAL_SCORING_STALE_SECONDS or age_seconds < -300
    before_reset = bool(reset_ts and report_cutoff and report_cutoff < reset_ts)
    trade_count_mismatch = bool(validated_paper_trades and scored_trades != validated_paper_trades)
    missing_watermark = not bool(scoring.get("snapshot_id") and scoring.get("report_cutoff") and scoring.get("metric_manifest_digest"))
    return {
        "real_scoring_missing": False,
        "real_scoring_passed": scoring.get("passed"),
        "real_scoring_snapshot_id": scoring.get("snapshot_id"),
        "real_scoring_metric_manifest_digest": scoring.get("metric_manifest_digest"),
        "real_scoring_as_of": scoring.get("as_of"),
        "real_scoring_report_cutoff": scoring.get("report_cutoff"),
        "real_scoring_age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
        "real_scoring_stale": stale,
        "real_scoring_stale_sla_seconds": REAL_SCORING_STALE_SECONDS,
        "real_scoring_before_account_reset": before_reset,
        "real_scoring_trade_count_mismatch": trade_count_mismatch,
        "real_scoring_scored_trades": scored_trades,
        "real_scoring_missing_watermark": missing_watermark,
    }


def evaluate_promotion(metrics: dict[str, Any], output_path: Path = PROMOTION_LATEST) -> dict[str, Any]:
    failures = []
    if int(metrics.get("paper_trades") or 0) < REQUIREMENTS["paper_trades"]:
        failures.append("insufficient_paper_trades")
    if int(metrics.get("shadow_closes") or 0) < REQUIREMENTS["shadow_closes"]:
        failures.append("insufficient_shadow_closes")
    if float(metrics.get("lifecycle_completeness") or 0.0) < REQUIREMENTS["lifecycle_completeness"]:
        failures.append("lifecycle_completeness_below_99pct")
    # daily_exam is DIAGNOSTIC-ONLY and must NOT block promotion (removed from the
    # hard gate). It may still be reported as advisory context, never a failure.
    if int(metrics.get("trial_days") or 0) < REQUIREMENTS["trial_days"]:
        failures.append("trial_too_short")
    if metrics.get("critical_dont_do_violation"):
        failures.append("critical_dont_do_violation")
    if metrics.get("portfolio_risk_status") == "critical":
        failures.append("portfolio_risk_critical")
    if metrics.get("real_scoring_required"):
        if metrics.get("real_scoring_missing"):
            failures.append("real_scoring_missing")
        if metrics.get("real_scoring_passed") is not True:
            failures.append("real_scoring_hard_gate_failed")
        if metrics.get("real_scoring_stale"):
            failures.append("real_scoring_stale")
        if metrics.get("real_scoring_before_account_reset"):
            failures.append("real_scoring_before_account_reset")
        if metrics.get("real_scoring_trade_count_mismatch"):
            failures.append("real_scoring_trade_count_mismatch")
        if metrics.get("real_scoring_missing_watermark"):
            failures.append("real_scoring_missing_watermark")
        if metrics.get("spend_adjusted_expectancy_negative"):
            failures.append("spend_adjusted_expectancy_negative")
        if metrics.get("real_scoring_manifest_mismatch"):
            failures.append("real_scoring_manifest_mismatch")
    if metrics.get("walk_forward_required"):
        if metrics.get("walk_forward_status") == "missing":
            failures.append("walk_forward_missing")
        if metrics.get("walk_forward_failed_patch_ids") or metrics.get("walk_forward_status") == "failed":
            failures.append("walk_forward_validation_failed")
        if metrics.get("walk_forward_inconclusive_patch_ids") or metrics.get("walk_forward_status") == "inconclusive":
            failures.append("walk_forward_validation_inconclusive")
        if metrics.get("walk_forward_running_patch_ids") or metrics.get("walk_forward_status") == "running":
            failures.append("walk_forward_validation_running")
        if metrics.get("walk_forward_stale"):
            failures.append("walk_forward_stale")
        if metrics.get("walk_forward_digest_mismatch_patch_ids"):
            failures.append("walk_forward_manifest_digest_mismatch")
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
    exam_history = read_jsonl(DAILY_EXAM_HISTORY)
    rolling_exam_rows = exam_history[-14:] if exam_history else []
    rolling_exam_avg = sum(float(row.get("quality_score") or row.get("exam_score") or 0.0) for row in rolling_exam_rows) / len(rolling_exam_rows) if rolling_exam_rows else float(exam.get("quality_score") or exam.get("exam_score") or 0.0)
    portfolio = read_json(MEMORY_DIR / "portfolio_risk_latest.json", default={})
    paper = read_json(STATE_DIR / "paper_account.json", default={})
    walk_forward = read_json(WALK_FORWARD_LATEST, default={})
    scoring = read_json(REAL_SCORING_LATEST, default={})
    active_patch_ids = [
        str(row.get("patch_id"))
        for row in read_jsonl(SKILL_PATCHES_APPLIED)
        if row.get("status") == "paper_only_applied" and row.get("patch_id")
    ]
    account_paper_trades = int(paper.get("closed_trades") or paper.get("trades") or 0)
    validated_paper_trades = validated_paper_closes_since_reset(paper)
    paper_trades = min(account_paper_trades, validated_paper_trades) if account_paper_trades else validated_paper_trades
    scoring_metrics = real_scoring_state_metrics(scoring, paper, paper_trades)
    metrics = {
        "paper_trades": paper_trades,
        "account_paper_trades": account_paper_trades,
        "validated_paper_trades": validated_paper_trades,
        "shadow_closes": int((shadow.get("overall") or {}).get("closed") or shadow.get("closed") or 0),
        "lifecycle_completeness": float(lifecycle.get("trade_lifecycle_completeness") or 0.0),
        "daily_exam_avg": float(rolling_exam_avg),
        "daily_exam_rolling_window": len(rolling_exam_rows) or (1 if exam else 0),
        "trial_days": int(paper.get("trial_days") or 0),
        "portfolio_risk_status": portfolio.get("status"),
        "real_scoring_required": True,
        **scoring_metrics,
        "spend_adjusted_expectancy_negative": "spend_adjusted_expectancy_negative" in (scoring.get("hard_errors") or []),
        "real_scoring_manifest_mismatch": bool(scoring.get("metric_manifest_digest") and scoring.get("metric_manifest", {}).get("digest") and scoring.get("metric_manifest_digest") != scoring.get("metric_manifest", {}).get("digest")),
        **walk_forward_metrics(walk_forward, active_patch_ids),
    }
    return evaluate_promotion(metrics, output_path)
