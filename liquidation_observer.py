"""Liquidation burst aggregation for replayable market context."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
LIQUIDATIONS_LATEST = STATE_DIR / "liquidations_latest.json"
LIQUIDATION_EVENTS = STATE_DIR / "liquidation_events.jsonl"


def event_id(symbol: str, side: str, ts: str, notional: float) -> str:
    return "liq_" + hashlib.sha256(f"{symbol}:{side}:{ts}:{notional}".encode("utf-8")).hexdigest()[:18]


def aggregate_liquidations(symbol: str, events: list[dict[str, Any]], burst_threshold_notional: float = 1_000_000.0) -> dict[str, Any]:
    long_liq = 0.0
    short_liq = 0.0
    rows = []
    for row in events:
        side = str(row.get("side") or "").upper()
        notional = float(row.get("notional") or 0.0)
        ts = str(row.get("ts") or utc_now())
        normalized = {"schema_version": SCHEMA_VERSION, "event_id": event_id(symbol.upper(), side, ts, notional), "ts": ts, "symbol": symbol.upper(), "side": side, "notional": notional}
        rows.append(normalized)
        if side in {"LONG", "BUY"}:
            long_liq += notional
        elif side in {"SHORT", "SELL"}:
            short_liq += notional
    total = long_liq + short_liq
    imbalance = (long_liq - short_liq) / total if total else 0.0
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": utc_now(),
        "symbol": symbol.upper(),
        "event_count": len(rows),
        "long_liquidation_notional": round(long_liq, 4),
        "short_liquidation_notional": round(short_liq, 4),
        "total_notional": round(total, 4),
        "imbalance": round(imbalance, 6),
        "burst": total >= burst_threshold_notional,
        "event_ids": [row["event_id"] for row in rows],
    }
    write_json_atomic(LIQUIDATIONS_LATEST, payload)
    for row in rows:
        append_jsonl(LIQUIDATION_EVENTS, row)
    return payload
