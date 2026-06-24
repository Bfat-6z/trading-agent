"""Walk-forward validation for paper-only skill patches.

This module evaluates whether an applied skill patch survives future paper
reviews after its application time. It is deliberately conservative: a patch is
`running` until enough out-of-sample reviews exist, and it never enables live
orders.
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Iterable

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, read_jsonl, write_json_atomic
from experiment_registry import EXPERIMENTS_JSONL, EXPERIMENTS_LATEST
from timebase import parse_utc, utc_now

ROOT = Path(__file__).resolve().parent
MEMORY_DIR = ROOT / "state" / "agent_memory"
POST_TRADE_REVIEWS = MEMORY_DIR / "post_trade_reviews.jsonl"
SKILL_PATCHES_APPLIED = MEMORY_DIR / "skill_patches_applied.jsonl"
SKILL_PATCHES_PENDING = MEMORY_DIR / "skill_patches_pending.jsonl"
WALK_FORWARD_LATEST = MEMORY_DIR / "walk_forward_latest.json"


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def setup_id_from_review(row: dict[str, Any]) -> str:
    source = row.get("source_trade") if isinstance(row.get("source_trade"), dict) else {}
    return str(source.get("setup_id") or row.get("setup_id") or "")


def review_ts(row: dict[str, Any]) -> Any:
    source = row.get("source_trade") if isinstance(row.get("source_trade"), dict) else {}
    return row.get("reviewed_at") or source.get("close_ts") or source.get("ts") or row.get("ts")


def review_net(row: dict[str, Any]) -> float:
    source = row.get("source_trade") if isinstance(row.get("source_trade"), dict) else {}
    costs = row.get("costs") if isinstance(row.get("costs"), dict) else {}
    return safe_float(source.get("net"), safe_float(costs.get("net")))


def filter_reviews(
    reviews: list[dict[str, Any]],
    setup_id: str,
    start: Any | None = None,
    end: Any | None = None,
    *,
    start_exclusive: bool = False,
) -> list[dict[str, Any]]:
    start_dt = parse_utc(start) if start else None
    end_dt = parse_utc(end) if end else None
    rows: list[dict[str, Any]] = []
    for row in reviews:
        if setup_id_from_review(row) != setup_id:
            continue
        ts = parse_utc(review_ts(row))
        if not ts:
            continue
        if start_dt and (ts <= start_dt if start_exclusive else ts < start_dt):
            continue
        if end_dt and ts > end_dt:
            continue
        rows.append(row)
    return rows


def performance_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    nets = [review_net(row) for row in rows]
    wins = [value for value in nets if value > 0]
    losses = [value for value in nets if value < 0]
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    regimes = set()
    for row, net in zip(rows, nets):
        equity += net
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)
        regime = row.get("market_regime")
        if regime:
            regimes.add(str(regime))
    trades = len(nets)
    net_total = sum(nets)
    return {
        "trades": trades,
        "net": round(net_total, 8),
        "expectancy_after_fees": round(net_total / trades, 8) if trades else 0.0,
        "profit_factor": round(sum(wins) / abs(sum(losses)), 4) if losses else (999.0 if wins else 0.0),
        "win_rate": round(len(wins) / trades, 4) if trades else 0.0,
        "avg_win": round(sum(wins) / len(wins), 8) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 8) if losses else 0.0,
        "max_drawdown": round(abs(max_dd), 8),
        "regime_coverage": sorted(regimes),
    }


def pending_patch_lookup(pending_rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(row.get("patch_id")): row for row in pending_rows if row.get("patch_id")}


def train_window_for_patch(patch: dict[str, Any], pending: dict[str, Any] | None, reviews: list[dict[str, Any]]) -> dict[str, Any]:
    setup_id = str(patch.get("setup_id") or "")
    applied_at = patch.get("applied_at")
    rows = filter_reviews(reviews, setup_id, end=applied_at)
    if rows:
        timestamps = [parse_utc(review_ts(row)) for row in rows if parse_utc(review_ts(row))]
        return {
            "start": min(timestamps).isoformat(timespec="seconds") if timestamps else None,
            "end": applied_at,
        }
    evidence = (pending or {}).get("evidence") if isinstance((pending or {}).get("evidence"), dict) else {}
    return {"start": evidence.get("start_ts"), "end": applied_at}


def evaluate_patch_walk_forward(
    patch: dict[str, Any],
    reviews: list[dict[str, Any]],
    pending: dict[str, Any] | None = None,
    min_test_trades: int = 20,
) -> dict[str, Any]:
    setup_id = str(patch.get("setup_id") or "")
    applied_at = patch.get("applied_at")
    experiment_id = "wf_" + str(patch.get("patch_id") or setup_id)
    if not setup_id or not parse_utc(applied_at):
        return {
            "schema_version": SCHEMA_VERSION,
            "experiment_id": experiment_id,
            "patch_id": patch.get("patch_id"),
            "setup_id": setup_id,
            "hypothesis": f"patch {patch.get('patch_id')} improves future paper expectancy",
            "status": "failed",
            "errors": ["missing_setup_id"] if not setup_id else ["invalid_patch_application_time"],
            "created_at": applied_at,
            "evaluated_at": utc_now(),
            "train_window": {"start": None, "end": applied_at},
            "test_window": {"start": applied_at, "end": utc_now()},
            "holdout": {"source": "future_post_trade_reviews", "not_used_for_patch_proposal": True},
            "train_metrics": performance_metrics([]),
            "test_metrics": performance_metrics([]),
            "min_test_trades": min_test_trades,
            "can_place_live_orders": False,
        }
    future_rows = filter_reviews(reviews, setup_id, start=applied_at, start_exclusive=True)
    past_rows = filter_reviews(reviews, setup_id, end=applied_at)
    train_metrics = performance_metrics(past_rows)
    test_metrics = performance_metrics(future_rows)
    errors: list[str] = []
    if test_metrics["trades"] < min_test_trades:
        errors.append("insufficient_future_trades")
    if test_metrics["trades"] >= min_test_trades and test_metrics["expectancy_after_fees"] <= 0:
        errors.append("future_expectancy_not_positive")
    if test_metrics["trades"] >= min_test_trades and test_metrics["profit_factor"] < 1.05:
        errors.append("future_profit_factor_too_low")
    status = "running" if test_metrics["trades"] < min_test_trades else "failed" if errors else "passed"
    return {
        "schema_version": SCHEMA_VERSION,
        "experiment_id": experiment_id,
        "patch_id": patch.get("patch_id"),
        "setup_id": setup_id,
        "hypothesis": f"patch {patch.get('patch_id')} improves future paper expectancy",
        "status": status,
        "errors": errors,
        "created_at": patch.get("applied_at"),
        "evaluated_at": utc_now(),
        "train_window": train_window_for_patch(patch, pending, reviews),
        "test_window": {"start": applied_at, "end": utc_now()},
        "holdout": {"source": "future_post_trade_reviews", "not_used_for_patch_proposal": True},
        "train_metrics": train_metrics,
        "test_metrics": test_metrics,
        "min_test_trades": min_test_trades,
        "can_place_live_orders": False,
    }


def latest_by_experiment(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered: list[str] = []
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.get("experiment_id") or "")
        if not key:
            continue
        if key not in latest:
            ordered.append(key)
        latest[key] = row
    return [latest[key] for key in ordered]


def write_outputs(results: list[dict[str, Any]], history_path: Path = EXPERIMENTS_JSONL, latest_path: Path = EXPERIMENTS_LATEST, walk_forward_path: Path = WALK_FORWARD_LATEST) -> dict[str, Any]:
    for row in results:
        append_jsonl(history_path, row)
    latest_rows = [
        row
        for row in latest_by_experiment(read_jsonl(history_path))
        if str(row.get("experiment_id") or "").startswith("wf_") and row.get("patch_id")
    ]
    by_status: dict[str, int] = {}
    for row in latest_rows:
        status = str(row.get("status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "experiment_count": len(latest_rows),
        "by_status": by_status,
        "rows": latest_rows[-30:],
        "can_place_live_orders": False,
    }
    write_json_atomic(latest_path, payload)
    write_json_atomic(walk_forward_path, payload)
    return payload


def run_once(
    *,
    applied_path: Path = SKILL_PATCHES_APPLIED,
    pending_path: Path = SKILL_PATCHES_PENDING,
    reviews_path: Path = POST_TRADE_REVIEWS,
    history_path: Path = EXPERIMENTS_JSONL,
    latest_path: Path = EXPERIMENTS_LATEST,
    walk_forward_path: Path = WALK_FORWARD_LATEST,
    min_test_trades: int = 20,
) -> dict[str, Any]:
    applied = read_jsonl(applied_path)
    pending_lookup = pending_patch_lookup(read_jsonl(pending_path))
    reviews = read_jsonl(reviews_path)
    results = [evaluate_patch_walk_forward(row, reviews, pending_lookup.get(str(row.get("patch_id"))), min_test_trades=min_test_trades) for row in applied]
    return write_outputs(results, history_path=history_path, latest_path=latest_path, walk_forward_path=walk_forward_path)


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Evaluate paper-only skill patches with walk-forward windows")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--min-test-trades", type=int, default=20)
    return parser.parse_args(list(argv) if argv is not None else None)


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_once(min_test_trades=args.min_test_trades)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
