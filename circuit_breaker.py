"""Circuit breakers that can only tighten/block autonomous paper actions."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
CIRCUIT_LATEST = ROOT / "state" / "agent_memory" / "circuit_breaker_latest.json"


def evaluate_circuit_breakers(metrics: dict[str, Any], output_path: Path = CIRCUIT_LATEST) -> dict[str, Any]:
    errors = []
    warnings = []
    if float(metrics.get("daily_loss_pct") or 0.0) <= -0.03:
        errors.append("daily_loss_cap_hit")
    if int(metrics.get("losing_streak") or 0) >= 5:
        errors.append("losing_streak_cap_hit")
    if float(metrics.get("max_slippage_bps") or 0.0) > 15:
        errors.append("slippage_breach")
    if metrics.get("source_status") in {"rate_limited", "outage"}:
        warnings.append("data_source_degraded")
    payload = {"schema_version": SCHEMA_VERSION, "checked_at": utc_now(), "allowed": not errors, "action": "block" if errors else "tighten_only" if warnings else "allow", "errors": errors, "warnings": warnings, "can_loosen_risk": False}
    write_json_atomic(output_path, payload)
    return payload
