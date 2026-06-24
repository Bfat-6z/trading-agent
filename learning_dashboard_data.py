"""Compact dashboard payload for Phase B learning panels."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from atomic_state import read_json, read_jsonl

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
MEMORY_DIR = STATE_DIR / "agent_memory"


def load_phase_b_learning() -> dict[str, Any]:
    lifecycle = read_json(MEMORY_DIR / "trade_lifecycle_latest.json", default={})
    post_trade = read_json(MEMORY_DIR / "post_trade_learning_latest.json", default={})
    counterfactual = read_json(MEMORY_DIR / "counterfactual_latest.json", default={})
    sources = read_json(STATE_DIR / "data_sources_latest.json", default={})
    regime = read_json(MEMORY_DIR / "regime_latest.json", default={})
    derivatives = read_json(STATE_DIR / "derivatives_latest.json", default={})
    orderbook = read_json(STATE_DIR / "orderbook_microstructure_latest.json", default={})
    liquidations = read_json(STATE_DIR / "liquidations_latest.json", default={})
    exploration = read_json(MEMORY_DIR / "paper_exploration_latest.json", default={})
    recent_reviews = read_jsonl(MEMORY_DIR / "post_trade_reviews.jsonl", limit=8)
    recent_replays = read_jsonl(MEMORY_DIR / "counterfactual_replays.jsonl", limit=8)
    return {
        "lifecycle": lifecycle,
        "post_trade": post_trade,
        "counterfactual": counterfactual,
        "sources": {"updated_at": sources.get("updated_at"), "source_count": len(sources.get("sources", {}) or {})},
        "regime": regime,
        "microstructure": {"derivatives": derivatives, "orderbook": orderbook, "liquidations": liquidations},
        "exploration": exploration,
        "recent_reviews": recent_reviews[-8:],
        "recent_replays": recent_replays[-8:],
    }
