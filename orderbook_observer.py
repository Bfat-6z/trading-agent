"""Orderbook microstructure evaluator for paper execution realism."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
ORDERBOOK_LATEST = STATE_DIR / "orderbook_microstructure_latest.json"


def levels_total(levels: list[list[Any]]) -> float:
    total = 0.0
    for level in levels:
        try:
            total += float(level[0]) * float(level[1])
        except Exception:
            continue
    return total


def evaluate_orderbook(
    symbol: str,
    bids: list[list[Any]],
    asks: list[list[Any]],
    max_spread_bps: float = 10.0,
    *,
    updated_at: str | None = None,
    last_update_id: int | None = None,
    previous_update_id: int | None = None,
    checksum_ok: bool | None = None,
) -> dict[str, Any]:
    best_bid = float(bids[0][0]) if bids else 0.0
    best_ask = float(asks[0][0]) if asks else 0.0
    mid = (best_bid + best_ask) / 2 if best_bid and best_ask else 0.0
    spread_bps = ((best_ask - best_bid) / mid * 10000) if mid else 999999.0
    bid_depth = levels_total(bids[:10])
    ask_depth = levels_total(asks[:10])
    total_depth = bid_depth + ask_depth
    imbalance = (bid_depth - ask_depth) / total_depth if total_depth else 0.0
    errors = []
    warnings = []
    if not bids or not asks:
        errors.append("missing_orderbook_side")
    if best_bid and best_ask and best_bid >= best_ask:
        errors.append("crossed_orderbook")
    if previous_update_id is not None and last_update_id is not None and last_update_id <= previous_update_id:
        errors.append("orderbook_update_id_gap")
    if checksum_ok is False:
        errors.append("orderbook_checksum_failed")
    if spread_bps > max_spread_bps:
        warnings.append("spread_spike")
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": updated_at or utc_now(),
        "symbol": symbol.upper(),
        "price_basis": "BOOK_MID",
        "depth_level": min(len(bids), len(asks)),
        "last_update_id": last_update_id,
        "previous_update_id": previous_update_id,
        "update_id_continuity": "unknown" if last_update_id is None or previous_update_id is None else "ok" if last_update_id > previous_update_id else "gap",
        "checksum_ok": checksum_ok,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "book_mid": round(mid, 10),
        "spread_bps": round(spread_bps, 4),
        "bid_depth_10": round(bid_depth, 4),
        "ask_depth_10": round(ask_depth, 4),
        "imbalance": round(imbalance, 6),
        "confidence": 0.0 if errors else 0.8 if not warnings else 0.55,
        "paper_entry_allowed": not errors and spread_bps <= max_spread_bps,
        "errors": errors,
        "warnings": warnings,
    }
    write_json_atomic(ORDERBOOK_LATEST, payload)
    return payload
