"""Paper capital allocation policy from setup rankings."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
ALLOCATION_LATEST = ROOT / "state" / "agent_memory" / "capital_allocation_latest.json"


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def allocate_capital(setup_id: str, rankings: list[dict[str, Any]], account: dict[str, Any], exploration_allowed: bool = False, output_path: Path = ALLOCATION_LATEST) -> dict[str, Any]:
    equity = safe_float(account.get("equity"), 100.0)
    lookup = {str(row.get("setup_id")): row for row in rankings}
    row = lookup.get(setup_id)
    errors = []
    tier = "shadow_only"
    risk_fraction = 0.0
    if not row:
        errors.append("setup_not_ranked")
    else:
        under_sampled = bool(row.get("under_sampled"))
        if under_sampled and not exploration_allowed:
            errors.append("setup_under_sampled")
        if safe_float(row.get("expectancy")) <= 0:
            errors.append("non_positive_expectancy")
        if not errors and under_sampled and exploration_allowed:
            tier = "exploration_paper"
            risk_fraction = 0.015
        elif not errors:
            rank_score = safe_float(row.get("rank_score"))
            tier = "normal_paper" if rank_score >= 0.6 else "tiny_paper"
            risk_fraction = 0.02 if tier == "normal_paper" else 0.0075
    if errors and exploration_allowed and row and safe_float(row.get("expectancy")) >= 0:
        tier = "exploration_paper"
        risk_fraction = 0.015
        errors = []
    payload = {"schema_version": SCHEMA_VERSION, "allocated_at": utc_now(), "setup_id": setup_id, "allowed": not errors, "tier": tier, "risk_fraction": risk_fraction, "max_loss_usdt": round(equity * risk_fraction, 6), "errors": errors, "can_trade_live": False}
    write_json_atomic(output_path, payload)
    return payload
