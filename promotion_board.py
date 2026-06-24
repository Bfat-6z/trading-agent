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
    }
    return evaluate_promotion(metrics, output_path)
