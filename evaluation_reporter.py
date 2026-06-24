"""Evaluation reporter comparing agent metrics against baselines."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
EVALUATION_LATEST = ROOT / "state" / "agent_memory" / "evaluation_latest.json"


def compare_to_baselines(agent: dict[str, Any], baselines: list[dict[str, Any]], min_trades: int = 30, output_path: Path = EVALUATION_LATEST) -> dict[str, Any]:
    agent_exp = float(agent.get("expectancy_after_fees") or agent.get("expectancy") or 0.0)
    best = max([float(row.get("expectancy_after_fees") or 0.0) for row in baselines], default=0.0)
    errors = []
    if int(agent.get("trades") or 0) < min_trades:
        errors.append("insufficient_agent_sample")
    if agent_exp <= best:
        errors.append("agent_does_not_beat_best_baseline")
    payload = {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "passed": not errors, "errors": errors, "agent_expectancy": agent_exp, "best_baseline_expectancy": best, "baselines": baselines, "agent": agent}
    write_json_atomic(output_path, payload)
    return payload
