"""Paper-only exploration budget to prevent over-conservative learning."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import read_json, write_json_atomic
from live_permission_firewall import evaluate_live_permission
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"
EXPLORATION_LATEST = MEMORY_DIR / "paper_exploration_latest.json"

DEFAULT_BUDGET_FRACTION = 0.10
MAX_EXPLORATION_MARGIN_FRACTION = 0.02


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def evaluate_exploration_request(
    signal: dict[str, Any],
    account: dict[str, Any],
    used_exploration_loss: float = 0.0,
    config: dict[str, Any] | None = None,
    output_path: Path = EXPLORATION_LATEST,
) -> dict[str, Any]:
    firewall = evaluate_live_permission({"action": "paper_exploration", **signal}, config or {"mode": "paper_learning", "live_execution_enabled": False, "feature_flags": {"live_orders": False}})
    equity = safe_float(account.get("equity"), 100.0)
    budget = equity * DEFAULT_BUDGET_FRACTION
    max_margin = equity * MAX_EXPLORATION_MARGIN_FRACTION
    requested_margin = safe_float(signal.get("margin"), max_margin)
    errors: list[str] = []
    warnings: list[str] = []
    if not firewall.get("allowed"):
        errors.extend(firewall.get("errors") or [])
    if used_exploration_loss >= budget:
        errors.append("exploration_budget_exhausted")
    if requested_margin > max_margin:
        warnings.append("margin_reduced_to_exploration_cap")
        requested_margin = max_margin
    if safe_float(signal.get("confidence"), 0.0) < 0.35:
        errors.append("signal_confidence_too_low_even_for_exploration")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "evaluated_at": utc_now(),
        "allowed": not errors,
        "mode": "paper_exploration_only",
        "budget": round(budget, 6),
        "used_exploration_loss": round(used_exploration_loss, 6),
        "approved_margin": round(requested_margin, 6) if not errors else 0.0,
        "max_margin": round(max_margin, 6),
        "errors": errors,
        "warnings": warnings,
        "reason": "ok" if not errors else ";".join(errors),
        "gate_change_allowed": False,
    }
    write_json_atomic(output_path, payload)
    return payload


def load_latest(path: Path = EXPLORATION_LATEST) -> dict[str, Any]:
    return read_json(path, default={})
