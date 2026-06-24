"""Evidence-based setup ranking for paper capital allocation."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
RANKINGS_LATEST = ROOT / "state" / "agent_memory" / "setup_rankings_latest.json"


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def rank_setup(row: dict[str, Any]) -> dict[str, Any]:
    trades = int(row.get("trades") or row.get("closed") or 0)
    expectancy = safe_float(row.get("expectancy"))
    profit_factor = safe_float(row.get("profit_factor"))
    win_rate = safe_float(row.get("win_rate"))
    max_drawdown = safe_float(row.get("max_drawdown"))
    confidence = min(1.0, trades / 50)
    score = expectancy * 10 + min(profit_factor, 3.0) * 0.2 + win_rate * 0.2 + confidence * 0.25 - max_drawdown * 0.5
    if trades < 20:
        score -= 0.5
    if expectancy <= 0:
        score -= 1.0
    return {**row, "rank_score": round(score, 6), "sample_confidence": round(confidence, 4), "under_sampled": trades < 20}


def rank_setups(rows: list[dict[str, Any]], output_path: Path = RANKINGS_LATEST) -> dict[str, Any]:
    ranked = [rank_setup(row) for row in rows]
    ranked.sort(key=lambda row: row["rank_score"], reverse=True)
    payload = {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "rankings": ranked, "top_setup_id": ranked[0].get("setup_id") if ranked else None}
    write_json_atomic(output_path, payload)
    return payload
