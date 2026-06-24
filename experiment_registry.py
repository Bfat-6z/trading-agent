"""Experiment registry and walk-forward-safe hypothesis tracking."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl_once, read_jsonl, write_json_atomic
from timebase import parse_utc, utc_now

ROOT = Path(__file__).resolve().parent
MEMORY_DIR = ROOT / "state" / "agent_memory"
EXPERIMENTS_JSONL = MEMORY_DIR / "experiments.jsonl"
EXPERIMENTS_LATEST = MEMORY_DIR / "experiments_latest.json"


def experiment_id(hypothesis: str, setup_id: str) -> str:
    return "exp_" + hashlib.sha256(f"{setup_id}:{hypothesis}".encode("utf-8")).hexdigest()[:20]


def windows_overlap(a: dict[str, str], b: dict[str, str]) -> bool:
    a0, a1 = parse_utc(a.get("start")), parse_utc(a.get("end"))
    b0, b1 = parse_utc(b.get("start")), parse_utc(b.get("end"))
    if not all([a0, a1, b0, b1]):
        return True
    return a0 <= b1 and b0 <= a1


def propose_experiment(hypothesis: str, setup_id: str, train_window: dict[str, str], test_window: dict[str, str], success_metric: str = "expectancy_after_fees", path: Path = EXPERIMENTS_JSONL) -> dict[str, Any]:
    errors = []
    if windows_overlap(train_window, test_window):
        errors.append("train_test_windows_overlap")
    row = {"schema_version": SCHEMA_VERSION, "experiment_id": experiment_id(hypothesis, setup_id), "hypothesis": hypothesis, "setup_id": setup_id, "train_window": train_window, "test_window": test_window, "success_metric": success_metric, "status": "rejected" if errors else "proposed", "errors": errors, "created_at": utc_now()}
    append_jsonl_once(path, row, "experiment_id")
    write_latest(path)
    return row


def evaluate_experiment(row: dict[str, Any], train_metrics: dict[str, Any], test_metrics: dict[str, Any], min_test_trades: int = 20) -> dict[str, Any]:
    metric = row.get("success_metric", "expectancy_after_fees")
    errors = []
    if int(test_metrics.get("trades") or 0) < min_test_trades:
        errors.append("insufficient_out_of_sample_trades")
    if float(test_metrics.get(metric) or 0.0) <= 0:
        errors.append("test_metric_not_positive")
    if float(train_metrics.get(metric) or 0.0) > 0 and float(test_metrics.get(metric) or 0.0) <= 0:
        errors.append("train_only_edge")
    status = "passed" if not errors else "failed"
    return {**row, "status": status, "evaluated_at": utc_now(), "train_metrics": train_metrics, "test_metrics": test_metrics, "errors": errors}


def write_latest(path: Path = EXPERIMENTS_JSONL, output_path: Path = EXPERIMENTS_LATEST) -> dict[str, Any]:
    rows = read_jsonl(path)
    by_status: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "unknown")
        by_status[status] = by_status.get(status, 0) + 1
    payload = {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "experiment_count": len(rows), "by_status": by_status}
    write_json_atomic(output_path, payload)
    return payload
