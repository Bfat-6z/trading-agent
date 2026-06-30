"""Confirmed swing-pivot detection for chart intelligence.

Pivots are decision-eligible only after the right-side confirmation candles are
closed and available. The module is paper-only evidence plumbing.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from agent_data_contracts import CHART_MODEL_VERSION, SCHEMA_VERSION, validate_chart_contract
from atomic_state import append_jsonl, canonical_json, write_json_atomic
from timebase import parse_utc, utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
PIVOT_DIR = STATE_DIR / "chart" / "pivots"


def stable_digest(prefix: str, payload: Any) -> str:
    return f"{prefix}_" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:20]


def safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def rounded(value: Any, digits: int = 10) -> float | None:
    number = safe_float(value)
    if number is None:
        return None
    return round(number, digits)


def iso_max(*values: Any) -> str | None:
    parsed = [dt for dt in (parse_utc(value) for value in values if value) if dt is not None]
    if not parsed:
        return None
    return max(parsed).isoformat(timespec="seconds")


def bar_known_at(bar: dict[str, Any]) -> str | None:
    return iso_max(bar.get("available_at"), bar.get("known_at"), bar.get("ingested_at"), bar.get("finalized_at"))


def candle_rows(candle_batch: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    rows: list[dict[str, Any]] = []
    cutoff_dt = parse_utc(candle_batch.get("decision_cutoff"))
    if not cutoff_dt:
        errors.append("invalid_decision_cutoff")
    for idx, bar in enumerate(candle_batch.get("bars") or []):
        if not isinstance(bar, dict):
            errors.append(f"invalid_bar:{idx}")
            continue
        if bar.get("is_final") is not True:
            errors.append(f"forming_candle:{idx}")
            continue
        high = safe_float(bar.get("high"))
        low = safe_float(bar.get("low"))
        open_price = safe_float(bar.get("open"))
        close = safe_float(bar.get("close"))
        known_at = bar_known_at(bar)
        known_dt = parse_utc(known_at)
        if not known_dt:
            errors.append(f"invalid_bar_known_at:{idx}")
            continue
        if cutoff_dt and known_dt > cutoff_dt:
            continue
        if None in (high, low, open_price, close):
            errors.append(f"invalid_ohlc:{idx}")
            continue
        rows.append(
            {
                "source_index": idx,
                "open_time": bar.get("open_time"),
                "close_time": bar.get("close_time"),
                "known_at": known_at,
                "open": float(open_price),
                "high": float(high),
                "low": float(low),
                "close": float(close),
                "volume": safe_float(bar.get("volume"), 0.0) or 0.0,
            }
        )
    rows.sort(key=lambda row: str(row.get("open_time") or ""))
    for sequence_index, row in enumerate(rows):
        row["sequence_index"] = sequence_index
    return rows, sorted(set(errors))


def rejection_strength(row: dict[str, Any], kind: str) -> float:
    high = float(row["high"])
    low = float(row["low"])
    open_price = float(row["open"])
    close = float(row["close"])
    span = high - low
    if span <= 0:
        return 0.0
    if kind == "high":
        wick = high - max(open_price, close)
    else:
        wick = min(open_price, close) - low
    return round(max(0.0, min(1.0, wick / span)), 6)


def confirmed_pivots(
    rows: list[dict[str, Any]],
    *,
    symbol: str,
    timeframe: str,
    decision_cutoff: str,
    left: int = 2,
    right: int = 2,
) -> tuple[list[dict[str, Any]], list[str]]:
    errors: list[str] = []
    if left < 1 or right < 1:
        return [], ["invalid_pivot_window"]
    cutoff_dt = parse_utc(decision_cutoff)
    if not cutoff_dt:
        return [], ["invalid_decision_cutoff"]
    pivots: list[dict[str, Any]] = []
    for idx in range(left, max(left, len(rows) - right)):
        row = rows[idx]
        left_rows = rows[idx - left : idx]
        right_rows = rows[idx + 1 : idx + right + 1]
        if len(left_rows) < left or len(right_rows) < right:
            continue
        confirmation = right_rows[-1]
        known_at = confirmation.get("known_at")
        known_dt = parse_utc(known_at)
        if not known_dt or known_dt > cutoff_dt:
            continue
        evidence_rows = left_rows + [row] + right_rows
        if any((parse_utc(peer.get("known_at")) is None or parse_utc(peer.get("known_at")) > cutoff_dt) for peer in evidence_rows):
            errors.append(f"pivot_evidence_after_cutoff:{row.get('open_time')}")
            continue
        high = float(row["high"])
        low = float(row["low"])
        high_is_pivot = all(high > float(peer["high"]) for peer in left_rows + right_rows)
        low_is_pivot = all(low < float(peer["low"]) for peer in left_rows + right_rows)
        for kind, price, is_pivot in (("high", high, high_is_pivot), ("low", low, low_is_pivot)):
            if not is_pivot:
                continue
            material = {
                "symbol": symbol.upper(),
                "timeframe": timeframe,
                "kind": kind,
                "pivot_open_time": row.get("open_time"),
                "pivot_close_time": row.get("close_time"),
                "price": rounded(price),
                "known_at": known_at,
                "left": left,
                "right": right,
            }
            pivots.append(
                {
                    "pivot_id": stable_digest("chart_pivot", material),
                    "symbol": symbol.upper(),
                    "timeframe": timeframe,
                    "kind": kind,
                    "price": rounded(price),
                    "candle_index": int(row["sequence_index"]),
                    "source_index": int(row["sequence_index"]),
                    "sequence_index": idx,
                    "candle_open_time": row.get("open_time"),
                    "candle_close_time": row.get("close_time"),
                    "open_time": row.get("open_time"),
                    "close_time": row.get("close_time"),
                    "known_at": known_at,
                    "confirmed_at": confirmation.get("close_time"),
                    "confirmed_known_at": known_at,
                    "confirmation_index": int(confirmation["sequence_index"]),
                    "confirmation_close_time": confirmation.get("close_time"),
                    "left_window": left,
                    "right_window": right,
                    "rejection_strength": rejection_strength(row, kind),
                    "strength": rejection_strength(row, kind),
                    "volume": rounded(row.get("volume"), 8),
                    "source_bar_ids": [
                        f"bar:{int(peer['sequence_index'])}:{peer.get('open_time')}"
                        for peer in evidence_rows
                    ],
                    "lookahead_guard": {
                        "requires_right_closed_candles": right,
                        "known_after_confirmation_index": int(confirmation["sequence_index"]),
                        "known_at_lte_decision_cutoff": True,
                    },
                }
            )
    return pivots, sorted(set(errors))


def compute_pivot_bundle(candle_batch: dict[str, Any], *, left: int = 2, right: int = 2, max_pivots: int = 120) -> dict[str, Any]:
    rows, row_errors = candle_rows(candle_batch)
    symbol = str(candle_batch.get("symbol") or "").upper()
    timeframe = str(candle_batch.get("timeframe") or "")
    errors = list(row_errors)
    raw_cutoff = candle_batch.get("decision_cutoff")
    if raw_cutoff in (None, ""):
        errors.append("missing_decision_cutoff")
    decision_cutoff = str(raw_cutoff or "1970-01-01T00:00:00+00:00")
    warnings: list[str] = []
    if len(rows) < left + right + 1:
        warnings.append("insufficient_pivot_history")
    pivots, pivot_errors = confirmed_pivots(rows, symbol=symbol, timeframe=timeframe, decision_cutoff=decision_cutoff, left=left, right=right)
    errors.extend(pivot_errors)
    selected = pivots[-max(0, int(max_pivots)) :]
    current_price = rounded(rows[-1]["close"]) if rows else None
    material = {
        "symbol": symbol,
        "timeframe": timeframe,
        "decision_cutoff": decision_cutoff,
        "left": left,
        "right": right,
        "pivot_ids": [pivot["pivot_id"] for pivot in selected],
    }
    structure_id = stable_digest("chart_pivots", material)
    source_ids = list(candle_batch.get("source_ids") or ["chart_candle_batch"])
    input_event_ids = list(candle_batch.get("input_event_ids") or [])
    if candle_batch.get("batch_id"):
        input_event_ids.append(str(candle_batch["batch_id"]))
    inherited_state = candle_batch.get("degradation_state")
    degradation_state = "quarantined" if errors or inherited_state == "quarantined" else "partial" if warnings or inherited_state == "partial" else "ok"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "chart_model_version": CHART_MODEL_VERSION,
        "contract": "ChartStructureBundle.v1",
        "structure_id": structure_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "price_basis": candle_batch.get("price_basis"),
        "native_timeframe": bool(candle_batch.get("native_timeframe", True)),
        "source_ids": source_ids,
        "input_event_ids": sorted(set(input_event_ids)),
        "decision_cutoff": decision_cutoff,
        "cutoff_proof": candle_batch.get("cutoff_proof") or {"ok": False, "errors": ["missing_cutoff_proof"]},
        "degradation_state": degradation_state,
        "structures": {
            "pivots": selected,
            "pivot_count": len(selected),
            "current_price": current_price,
            "candle_count": len(rows),
            "pivot_policy": {
                "left_window": left,
                "right_window": right,
                "closed_candles_only": True,
                "confirmation": "right_closed_candles",
                "max_pivots": max_pivots,
            },
        },
        "capability_mask": {
            "action": "normal" if degradation_state == "ok" else "size_cap" if degradation_state == "partial" else "skip",
            "value_errors": sorted(set(errors)),
            "warnings": sorted(set(warnings)),
            "source_confidence": 1.0 if degradation_state == "ok" else 0.5 if degradation_state == "partial" else 0.0,
        },
        "created_at": utc_now(),
        "can_place_live_orders": False,
        "live_permission": False,
    }
    validation = validate_chart_contract("ChartStructureBundle.v1", payload)
    if not validation.ok:
        payload["degradation_state"] = "quarantined"
        payload["capability_mask"]["action"] = "skip"
        payload["capability_mask"]["value_errors"] = sorted(set(payload["capability_mask"]["value_errors"] + validation.errors))
    return payload


def pivot_path(symbol: str, timeframe: str) -> Path:
    return PIVOT_DIR / symbol.upper() / f"{timeframe}.jsonl"


def pivot_latest_path(symbol: str, timeframe: str) -> Path:
    return PIVOT_DIR / symbol.upper() / f"{timeframe}.latest.json"


def store_pivot_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    append_jsonl(pivot_path(str(bundle.get("symbol") or ""), str(bundle.get("timeframe") or "")), bundle)
    write_json_atomic(pivot_latest_path(str(bundle.get("symbol") or ""), str(bundle.get("timeframe") or "")), bundle)
    return bundle
