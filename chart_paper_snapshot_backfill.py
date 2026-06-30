"""Diagnostic-only backfill helpers for paper trade chart snapshot lineage."""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any, Iterable

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, read_jsonl, write_json_atomic
from paper_execution_lifecycle_loop import PAPER_TRADES_PATH, stable_digest
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
BACKFILL_PATH = MEMORY_DIR / "paper_chart_snapshot_backfill.jsonl"
LATEST_PATH = MEMORY_DIR / "paper_chart_snapshot_backfill_latest.json"

def trade_needs_required_chart_snapshot(row: dict[str, Any]) -> bool:
    if not isinstance(row, dict):
        return False
    if row.get("event") not in {"paper_open", "paper_close"}:
        return False
    return bool(row.get("chart_score_id") or row.get("chart_risk_plan_id") or row.get("chart_intelligence_id"))

def snapshot_ids(row: dict[str, Any]) -> dict[str, Any]:
    ids = row.get("chart_snapshot_ids")
    return ids if isinstance(ids, dict) else {}

def reconcile_chart_snapshots(rows: list[dict[str, Any]]) -> dict[str, Any]:
    missing_required: list[dict[str, Any]] = []
    diagnostic_rows = 0
    for row in rows:
        ids = snapshot_ids(row)
        if row.get("diagnostic_only"):
            diagnostic_rows += 1
            continue
        if trade_needs_required_chart_snapshot(row) and not ids:
            missing_required.append({"trade_id": row.get("trade_id"), "event": row.get("event"), "reason": "missing_required_chart_snapshot_ids"})
    return {
        "schema_version": SCHEMA_VERSION,
        "checked_at": utc_now(),
        "missing_required_count": len(missing_required),
        "missing_required": missing_required[-100:],
        "diagnostic_rows": diagnostic_rows,
        "can_place_live_orders": False,
    }

def build_diagnostic_backfill(row: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(row, dict) or row.get("event") not in {"paper_open", "paper_close"}:
        return None
    if snapshot_ids(row):
        return None
    trade_id = row.get("trade_id") or row.get("paper_trade_id")
    if not trade_id:
        return None
    stage = "close" if row.get("event") == "paper_close" else "open"
    snapshot_id = stable_digest(
        "diagnostic_paper_chart_snapshot",
        {
            "trade_id": trade_id,
            "stage": stage,
            "symbol": row.get("symbol"),
            "ts": row.get("close_ts") or row.get("open_ts") or row.get("ts"),
        },
    )
    return {
        "schema_version": SCHEMA_VERSION,
        "contract": "PaperChartSnapshotBackfill.v1",
        "backfill_id": stable_digest("paper_chart_snapshot_backfill", {"trade_id": trade_id, "stage": stage, "snapshot_id": snapshot_id}),
        "trade_id": trade_id,
        "event": row.get("event"),
        "stage": stage,
        "symbol": row.get("symbol"),
        "chart_snapshot_ids": {stage: snapshot_id},
        "diagnostic_only": True,
        "readiness_eligible": False,
        "learning_eligible": False,
        "reason": "historical_trade_missing_original_chart_artifact",
        "created_at": utc_now(),
        "can_place_live_orders": False,
    }

def backfill_paper_trade_snapshots(
    *,
    trades_path: Path = PAPER_TRADES_PATH,
    output_path: Path = BACKFILL_PATH,
    latest_path: Path = LATEST_PATH,
) -> dict[str, Any]:
    rows = read_jsonl(trades_path)
    backfilled = [row for row in (build_diagnostic_backfill(row) for row in rows) if row]
    for row in backfilled:
        append_jsonl(output_path, row)
    report = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "source_path": str(trades_path),
        "output_path": str(output_path),
        "backfilled_count": len(backfilled),
        "backfilled_ids": [row["backfill_id"] for row in backfilled[-100:]],
        "reconciliation": reconcile_chart_snapshots(rows + backfilled),
        "diagnostic_only": True,
        "can_place_live_orders": False,
    }
    write_json_atomic(latest_path, report)
    return report

def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Backfill diagnostic-only paper chart snapshot ids")
    parser.add_argument("--trades-path", type=Path, default=PAPER_TRADES_PATH)
    parser.add_argument("--output-path", type=Path, default=BACKFILL_PATH)
    parser.add_argument("--latest-path", type=Path, default=LATEST_PATH)
    return parser.parse_args(list(argv) if argv is not None else None)

def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    report = backfill_paper_trade_snapshots(trades_path=args.trades_path, output_path=args.output_path, latest_path=args.latest_path)
    print(report)
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
