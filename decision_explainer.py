"""Decision drilldown loader for trade/setup/memory/source ids."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import read_json, read_jsonl
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"

SOURCES = [
    ("paper_brain", MEMORY_DIR / "paper_trading_brain_history.jsonl", ["trade_id", "signal_id", "setup_id"]),
    ("post_trade_review", MEMORY_DIR / "post_trade_reviews.jsonl", ["review_id", "trade_id", "setup_id"]),
    ("counterfactual", MEMORY_DIR / "counterfactual_replays.jsonl", ["replay_id", "signal_id"]),
    ("external_signal", MEMORY_DIR / "external_signals.jsonl", ["signal_id", "source_id"]),
    ("promoted_memory", MEMORY_DIR / "memory_promoted.jsonl", ["memory_id", "candidate_id"]),
]


def find_rows(identifier: str, limit: int = 20) -> list[dict[str, Any]]:
    matches = []
    for kind, path, fields in SOURCES:
        for row in read_jsonl(path):
            if any(str(row.get(field)) == identifier for field in fields):
                matches.append({"kind": kind, "path": str(path), "row": row})
                if len(matches) >= limit:
                    return matches
    return matches


def explain_decision(identifier: str) -> dict[str, Any]:
    matches = find_rows(identifier)
    latest = {
        "promotion": read_json(MEMORY_DIR / "promotion_board_latest.json", default={}),
        "preflight": read_json(STATE_DIR / "preflight_latest.json", default={}),
        "circuit_breaker": read_json(MEMORY_DIR / "circuit_breaker_latest.json", default={}),
    }
    reasons = []
    for item in matches:
        row = item["row"]
        for key in ("reason", "classification", "conclusion", "action"):
            if row.get(key):
                reasons.append({"kind": item["kind"], "reason": row[key]})
    return {"schema_version": SCHEMA_VERSION, "explained_at": utc_now(), "identifier": identifier, "match_count": len(matches), "matches": matches, "reasons": reasons, "latest_context": latest}
