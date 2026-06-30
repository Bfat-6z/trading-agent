"""Paper account reconciliation from canonical paper trade ledger."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import read_json, read_jsonl, write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
PAPER_ACCOUNT = STATE_DIR / "paper_account.json"
PAPER_TRADES = MEMORY_DIR / "paper_trades.jsonl"
RECONCILE_LATEST = MEMORY_DIR / "paper_account_reconciliation_latest.json"

def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default

def ledger_id(rows: list[dict[str, Any]]) -> str:
    raw = json.dumps(rows, ensure_ascii=True, sort_keys=True, separators=(",", ":"), default=str)
    return "paper_ledger_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]

def trade_id(row: dict[str, Any]) -> str:
    return str(row.get("trade_id") or row.get("paper_trade_id") or row.get("position_id") or "")

def rebuild_from_paper_ledger(account: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    start_equity = safe_float(account.get("starting_equity"), safe_float(account.get("initial_equity"), safe_float(account.get("genesis_equity"), 100.0)))
    equity = start_equity
    open_positions: dict[str, dict[str, Any]] = {}
    orphan_closes = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        event = str(row.get("event") or row.get("status") or "")
        tid = trade_id(row)
        if event == "paper_open" or row.get("status") == "open":
            if tid:
                open_positions[tid] = row
        if event == "paper_close" or row.get("status") == "closed":
            equity += safe_float(row.get("net"), safe_float(row.get("pnl"), safe_float(row.get("realized_pnl"), 0.0)))
            if tid and tid in open_positions:
                open_positions.pop(tid, None)
            elif tid:
                orphan_closes += 1
    return {
        "starting_equity": round(start_equity, 8),
        "rebuilt_equity": round(equity, 8),
        "open_positions": list(open_positions.values()),
        "open_position_count": len(open_positions),
        "orphan_closes": orphan_closes,
        "closed_count": sum(1 for row in rows if str(row.get("event") or row.get("status") or "") in {"paper_close", "closed"}),
    }

def reconcile_paper_account(
    account_path: Path = PAPER_ACCOUNT,
    trades_path: Path = PAPER_TRADES,
    output_path: Path = RECONCILE_LATEST,
    *,
    latest_account: dict[str, Any] | None = None,
    restore_output: dict[str, Any] | None = None,
    tolerance: float = 1e-6,
) -> dict[str, Any]:
    account = latest_account if isinstance(latest_account, dict) else read_json(account_path, default={})
    rows = read_jsonl(trades_path)
    rebuilt = rebuild_from_paper_ledger(account, rows)
    latest_equity = safe_float(account.get("equity"), rebuilt["starting_equity"])
    latest_positions = account.get("open_positions") if isinstance(account.get("open_positions"), list) else []
    errors: list[str] = []
    observed_environment = str(account.get("environment") or "paper").lower()
    observed_account_scope = str(account.get("account_scope") or "paper").lower()
    if observed_account_scope in {"live", "real", "production", "mainnet"} or observed_environment in {"live", "real", "production", "mainnet"}:
        errors.append("live_account_snapshot_forbidden")
    if abs(latest_equity - rebuilt["rebuilt_equity"]) > tolerance:
        errors.append("equity_drift")
    if len(latest_positions) != rebuilt["open_position_count"]:
        errors.append("open_position_count_drift")
    if rebuilt["orphan_closes"]:
        errors.append("orphan_close_in_ledger")
    if restore_output is not None and not bool(restore_output.get("ok")):
        errors.append("restore_output_not_ok")
    source_ledger_id = ledger_id(rows)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "checked_at": utc_now(),
        "ok": not errors,
        "errors": sorted(set(errors)),
        "source_mode": "paper_ledger",
        "environment": observed_environment,
        "account_scope": observed_account_scope,
        "credential_fingerprint": "none",
        "source_ledger_id": source_ledger_id,
        "latest_equity": round(latest_equity, 8),
        "rebuilt": rebuilt,
        "restore_output_checked": restore_output is not None,
        "can_place_live_orders": False,
        "live_permission": False,
    }
    write_json_atomic(output_path, payload)
    return payload
