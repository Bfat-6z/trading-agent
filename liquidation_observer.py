"""Liquidation burst aggregation for replayable market context."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import append_jsonl, write_json_atomic
from timebase import parse_utc, utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
LIQUIDATIONS_LATEST = STATE_DIR / "liquidations_latest.json"
LIQUIDATION_EVENTS = STATE_DIR / "liquidation_events.jsonl"


def event_id(symbol: str, side: str, ts: str, notional: float) -> str:
    return "liq_" + hashlib.sha256(f"{symbol}:{side}:{ts}:{notional}".encode("utf-8")).hexdigest()[:18]


def aggregate_liquidations(
    symbol: str,
    events: list[dict[str, Any]],
    burst_threshold_notional: float = 1_000_000.0,
    *,
    decision_cutoff: str | None = None,
    reference_price: Any = None,
    proximity_bps: float = 50.0,
    feed_status: str = "ok",
) -> dict[str, Any]:
    as_of = decision_cutoff or utc_now()
    long_liq = 0.0
    short_liq = 0.0
    rows = []
    excluded_after_cutoff = 0
    cutoff_dt = parse_utc(decision_cutoff) if decision_cutoff else None
    ref_price = float(reference_price or 0.0)
    for row in events:
        side = str(row.get("side") or "").upper()
        notional = float(row.get("notional") or 0.0)
        ts = str(row.get("ts") or utc_now())
        available_at = str(row.get("available_at") or row.get("known_at") or ts)
        if cutoff_dt and parse_utc(available_at) and parse_utc(available_at) > cutoff_dt:
            excluded_after_cutoff += 1
            continue
        price = float(row.get("price") or row.get("mark_price") or 0.0)
        distance_bps = abs((price - ref_price) / ref_price * 10000.0) if price > 0 and ref_price > 0 else None
        near_price = distance_bps is not None and distance_bps <= proximity_bps
        normalized = {
            "schema_version": SCHEMA_VERSION,
            "event_id": event_id(symbol.upper(), side, ts, notional),
            "ts": ts,
            "available_at": available_at,
            "symbol": symbol.upper(),
            "side": side,
            "notional": notional,
            "price": price or None,
            "price_basis": "MARK",
            "distance_bps": round(distance_bps, 4) if distance_bps is not None else None,
            "near_price": near_price,
        }
        rows.append(normalized)
        if side in {"LONG", "BUY"}:
            long_liq += notional
        elif side in {"SHORT", "SELL"}:
            short_liq += notional
    total = long_liq + short_liq
    imbalance = (long_liq - short_liq) / total if total else 0.0
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": as_of,
        "symbol": symbol.upper(),
        "feed_status": feed_status,
        "coverage": "unknown" if feed_status != "ok" else "partial" if excluded_after_cutoff else "observed",
        "decision_cutoff": decision_cutoff,
        "window": {"start": rows[0]["ts"] if rows else None, "end": as_of, "type": "half_open_to_decision_cutoff"},
        "event_count": len(rows),
        "excluded_after_cutoff": excluded_after_cutoff,
        "long_liquidation_notional": round(long_liq, 4),
        "short_liquidation_notional": round(short_liq, 4),
        "total_notional": round(total, 4),
        "imbalance": round(imbalance, 6),
        "burst": total >= burst_threshold_notional,
        "near_price_event_count": sum(1 for row in rows if row.get("near_price")),
        "event_ids": [row["event_id"] for row in rows],
        "usable_for_features": feed_status == "ok",
    }
    write_json_atomic(LIQUIDATIONS_LATEST, payload)
    for row in rows:
        append_jsonl(LIQUIDATION_EVENTS, row)
    return payload
