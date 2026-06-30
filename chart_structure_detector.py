"""Market-structure detection from confirmed pivots and closed candles."""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from agent_data_contracts import CHART_MODEL_VERSION, SCHEMA_VERSION, validate_chart_contract
from atomic_state import append_jsonl, canonical_json, write_json_atomic
from timebase import parse_utc, utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
STRUCTURE_DIR = STATE_DIR / "chart" / "structure"

DEFAULT_SIGNIFICANCE_PCT = 0.0025
DEFAULT_ATR_MULTIPLE = 0.20


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


def extract_bars(candle_batch: dict[str, Any] | None, *, decision_cutoff: str | None) -> list[dict[str, Any]]:
    cutoff_dt = parse_utc(decision_cutoff)
    rows: list[dict[str, Any]] = []
    if not candle_batch or not cutoff_dt:
        return rows
    for bar in candle_batch.get("bars") or []:
        if not isinstance(bar, dict) or bar.get("is_final") is not True:
            continue
        known_dt = parse_utc(bar_known_at(bar))
        if not known_dt or known_dt > cutoff_dt:
            continue
        high = safe_float(bar.get("high"))
        low = safe_float(bar.get("low"))
        close = safe_float(bar.get("close"))
        if None in (high, low, close):
            continue
        rows.append(
            {
                "open_time": bar.get("open_time"),
                "close_time": bar.get("close_time"),
                "high": float(high),
                "low": float(low),
                "close": float(close),
            }
        )
    rows.sort(key=lambda row: str(row.get("open_time") or ""))
    for idx, row in enumerate(rows):
        row["sequence_index"] = idx
    return rows


def atr_from_indicator(indicator_bundle: dict[str, Any] | None) -> float | None:
    indicators = indicator_bundle.get("indicators") if isinstance(indicator_bundle, dict) and isinstance(indicator_bundle.get("indicators"), dict) else {}
    return safe_float(indicators.get("atr14"))


def significance_threshold(reference_price: float, atr: float | None, *, significance_pct: float, atr_multiple: float) -> float:
    return max(abs(reference_price) * significance_pct, (atr or 0.0) * atr_multiple, 0.0)


def confirmed_pivots(pivot_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    cutoff_dt = parse_utc(pivot_bundle.get("decision_cutoff"))
    structures = pivot_bundle.get("structures") if isinstance(pivot_bundle.get("structures"), dict) else {}
    rows: list[dict[str, Any]] = []
    if not cutoff_dt:
        return rows
    for pivot in structures.get("pivots", []) if isinstance(structures.get("pivots"), list) else []:
        if not isinstance(pivot, dict):
            continue
        known_dt = parse_utc(pivot.get("confirmed_known_at") or pivot.get("known_at"))
        price = safe_float(pivot.get("price"))
        x = pivot.get("sequence_index", pivot.get("source_index", pivot.get("candle_index")))
        if not known_dt or known_dt > cutoff_dt or price is None or x is None:
            continue
        guard = pivot.get("lookahead_guard") if isinstance(pivot.get("lookahead_guard"), dict) else {}
        if guard.get("known_at_lte_decision_cutoff") is not True:
            continue
        row = dict(pivot)
        row["x"] = int(x)
        row["price"] = float(price)
        rows.append(row)
    rows.sort(key=lambda item: (int(item["x"]), 0 if item.get("kind") == "low" else 1, str(item.get("pivot_id") or "")))
    return rows


def label_pivots(pivots: list[dict[str, Any]], threshold: float) -> list[dict[str, Any]]:
    last_by_kind: dict[str, dict[str, Any]] = {}
    labeled: list[dict[str, Any]] = []
    for pivot in pivots:
        kind = str(pivot.get("kind"))
        previous = last_by_kind.get(kind)
        label = "first_high" if kind == "high" else "first_low"
        delta = None
        if previous:
            delta = float(pivot["price"]) - float(previous["price"])
            if kind == "high":
                label = "HH" if delta > threshold else "LH" if delta < -threshold else "EH"
            elif kind == "low":
                label = "HL" if delta > threshold else "LL" if delta < -threshold else "EL"
        row = dict(pivot)
        row["structure_label"] = label
        row["delta_vs_previous_same_kind"] = rounded(delta)
        labeled.append(row)
        if kind in {"high", "low"}:
            last_by_kind[kind] = pivot
    return labeled


def recent_labels(labeled: list[dict[str, Any]], kind: str, count: int = 3) -> list[str]:
    return [str(row.get("structure_label")) for row in labeled if row.get("kind") == kind][-count:]


def infer_prior_trend(labeled: list[dict[str, Any]]) -> str:
    highs = recent_labels(labeled, "high")
    lows = recent_labels(labeled, "low")
    if any(label == "HH" for label in highs[-2:]) and any(label == "HL" for label in lows[-2:]):
        return "uptrend"
    if any(label == "LH" for label in highs[-2:]) and any(label == "LL" for label in lows[-2:]):
        return "downtrend"
    return "range"


def last_pivot(labeled: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    rows = [row for row in labeled if row.get("kind") == kind]
    return rows[-1] if rows else None


def previous_pivot(labeled: list[dict[str, Any]], kind: str) -> dict[str, Any] | None:
    rows = [row for row in labeled if row.get("kind") == kind]
    return rows[-2] if len(rows) >= 2 else None


def close_breaks_level(close: float, level: float | None, threshold: float, direction: str) -> bool:
    if level is None:
        return False
    if direction == "up":
        return close > level + threshold
    return close < level - threshold


def wick_sweeps_level(bar: dict[str, Any], level: float | None, threshold: float, direction: str) -> bool:
    if level is None:
        return False
    if direction == "up":
        return float(bar["high"]) > level + threshold and float(bar["close"]) <= level + threshold
    return float(bar["low"]) < level - threshold and float(bar["close"]) >= level - threshold


def detect_breaks(labeled: list[dict[str, Any]], bars: list[dict[str, Any]], threshold: float, prior_trend: str) -> tuple[list[dict[str, Any]], list[str]]:
    if not bars:
        return [], []
    last_bar = bars[-1]
    close = float(last_bar["close"])
    high_ref = last_pivot(labeled, "high")
    low_ref = last_pivot(labeled, "low")
    high_level = float(high_ref["price"]) if high_ref else None
    low_level = float(low_ref["price"]) if low_ref else None
    events: list[dict[str, Any]] = []
    reason_codes: list[str] = []
    if close_breaks_level(close, high_level, threshold, "up"):
        event_type = "CHOCH_UP" if prior_trend == "downtrend" else "BOS_UP"
        reason_codes.append("choch_up" if event_type == "CHOCH_UP" else "bos_up")
        events.append(
            {
                "event_type": event_type,
                "side": "bullish",
                "level": rounded(high_level),
                "reference_pivot_id": high_ref.get("pivot_id") if high_ref else None,
                "confirmed_by": "close",
                "close": rounded(close),
                "bar_close_time": last_bar.get("close_time"),
            }
        )
    elif wick_sweeps_level(last_bar, high_level, threshold, "up"):
        events.append({"event_type": "WICK_SWEEP_UP", "side": "bearish", "level": rounded(high_level), "reference_pivot_id": high_ref.get("pivot_id") if high_ref else None, "confirmed_by": "wick_only", "bar_close_time": last_bar.get("close_time")})
    if close_breaks_level(close, low_level, threshold, "down"):
        event_type = "CHOCH_DOWN" if prior_trend == "uptrend" else "BOS_DOWN"
        reason_codes.append("choch_down" if event_type == "CHOCH_DOWN" else "bos_down")
        events.append(
            {
                "event_type": event_type,
                "side": "bearish",
                "level": rounded(low_level),
                "reference_pivot_id": low_ref.get("pivot_id") if low_ref else None,
                "confirmed_by": "close",
                "close": rounded(close),
                "bar_close_time": last_bar.get("close_time"),
            }
        )
    elif wick_sweeps_level(last_bar, low_level, threshold, "down"):
        events.append({"event_type": "WICK_SWEEP_DOWN", "side": "bullish", "level": rounded(low_level), "reference_pivot_id": low_ref.get("pivot_id") if low_ref else None, "confirmed_by": "wick_only", "bar_close_time": last_bar.get("close_time")})
    return events, reason_codes


def range_state(labeled: list[dict[str, Any]], threshold: float) -> dict[str, Any]:
    highs = [row for row in labeled if row.get("kind") == "high"][-3:]
    lows = [row for row in labeled if row.get("kind") == "low"][-3:]
    if len(highs) < 2 or len(lows) < 2:
        return {"is_range": False, "high": None, "low": None, "width": None}
    high_prices = [float(row["price"]) for row in highs]
    low_prices = [float(row["price"]) for row in lows]
    high_compressed = max(high_prices) - min(high_prices) <= threshold * 3
    low_compressed = max(low_prices) - min(low_prices) <= threshold * 3
    return {
        "is_range": bool(high_compressed and low_compressed),
        "high": rounded(max(high_prices)),
        "low": rounded(min(low_prices)),
        "width": rounded(max(high_prices) - min(low_prices)),
    }


def side_bias(prior_trend: str, events: list[dict[str, Any]], range_info: dict[str, Any]) -> str:
    close_events = [event for event in events if event.get("confirmed_by") == "close"]
    if close_events:
        last = close_events[-1]
        return "bullish" if str(last.get("side")) == "bullish" else "bearish"
    if range_info.get("is_range"):
        return "neutral"
    if prior_trend == "uptrend":
        return "bullish"
    if prior_trend == "downtrend":
        return "bearish"
    return "neutral"


def invalidation_level(bias: str, labeled: list[dict[str, Any]], range_info: dict[str, Any]) -> float | None:
    if bias == "bullish":
        ref = last_pivot(labeled, "low")
        return rounded(ref.get("price")) if ref else safe_float(range_info.get("low"))
    if bias == "bearish":
        ref = last_pivot(labeled, "high")
        return rounded(ref.get("price")) if ref else safe_float(range_info.get("high"))
    return None


def compute_market_structure_bundle(
    pivot_bundle: dict[str, Any],
    *,
    candle_batch: dict[str, Any] | None = None,
    indicator_bundle: dict[str, Any] | None = None,
    significance_pct: float = DEFAULT_SIGNIFICANCE_PCT,
    atr_multiple: float = DEFAULT_ATR_MULTIPLE,
) -> dict[str, Any]:
    symbol = str(pivot_bundle.get("symbol") or "").upper()
    timeframe = str(pivot_bundle.get("timeframe") or "")
    decision_cutoff = str(pivot_bundle.get("decision_cutoff") or "")
    errors: list[str] = []
    warnings: list[str] = []
    bars = extract_bars(candle_batch, decision_cutoff=decision_cutoff)
    structures = pivot_bundle.get("structures") if isinstance(pivot_bundle.get("structures"), dict) else {}
    current_price = float(bars[-1]["close"]) if bars else safe_float(structures.get("current_price"), 0.0) or 0.0
    if current_price <= 0:
        errors.append("invalid_current_price")
    atr = atr_from_indicator(indicator_bundle)
    threshold = significance_threshold(current_price, atr, significance_pct=significance_pct, atr_multiple=atr_multiple)
    pivots = confirmed_pivots(pivot_bundle)
    if len(pivots) < 4:
        warnings.append("insufficient_pivots_for_structure")
    labeled = label_pivots(pivots, threshold)
    trend = infer_prior_trend(labeled)
    events, reason_codes = detect_breaks(labeled, bars, threshold, trend)
    range_info = range_state(labeled, threshold)
    bias = side_bias(trend, events, range_info)
    invalidation = invalidation_level(bias, labeled, range_info)
    confidence = 0.35
    if trend in {"uptrend", "downtrend"}:
        confidence += 0.2
    if any(event.get("confirmed_by") == "close" for event in events):
        confidence += 0.25
    if range_info.get("is_range"):
        confidence += 0.1
    if len(pivots) < 4:
        confidence -= 0.15
    confidence = round(max(0.0, min(1.0, confidence)), 4)
    material = {
        "source_structure_id": pivot_bundle.get("structure_id"),
        "labels": [(row.get("pivot_id"), row.get("structure_label")) for row in labeled],
        "events": events,
        "bias": bias,
        "threshold": rounded(threshold),
    }
    structure_id = stable_digest("chart_structure", material)
    source_ids = list(pivot_bundle.get("source_ids") or ["chart_pivot_detector"])
    input_event_ids = list(pivot_bundle.get("input_event_ids") or [])
    if pivot_bundle.get("structure_id"):
        input_event_ids.append(str(pivot_bundle["structure_id"]))
    inherited_state = pivot_bundle.get("degradation_state")
    degradation_state = "quarantined" if errors or inherited_state == "quarantined" else "partial" if warnings or inherited_state == "partial" else "ok"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "chart_model_version": CHART_MODEL_VERSION,
        "contract": "ChartStructureBundle.v1",
        "structure_id": structure_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "price_basis": pivot_bundle.get("price_basis"),
        "native_timeframe": bool(pivot_bundle.get("native_timeframe", True)),
        "source_structure_id": pivot_bundle.get("structure_id"),
        "source_ids": source_ids,
        "input_event_ids": sorted(set(input_event_ids)),
        "decision_cutoff": pivot_bundle.get("decision_cutoff"),
        "cutoff_proof": pivot_bundle.get("cutoff_proof") or {"ok": False, "errors": ["missing_cutoff_proof"]},
        "degradation_state": degradation_state,
        "structures": {
            "pivots": labeled,
            "structure_events": events,
            "reason_codes": sorted(set(reason_codes)),
            "trend_state": trend,
            "side_bias": bias,
            "confidence": confidence,
            "invalidation_level": invalidation,
            "range": range_info,
            "current_price": rounded(current_price),
            "structure_policy": {
                "significance_pct": significance_pct,
                "atr_multiple": atr_multiple,
                "effective_threshold": rounded(threshold),
                "bos_requires": "close_beyond_level",
                "wick_only": "sweep_only_not_bos",
                "closed_candles_only": True,
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


def structure_path(symbol: str, timeframe: str) -> Path:
    return STRUCTURE_DIR / symbol.upper() / f"{timeframe}.jsonl"


def structure_latest_path(symbol: str, timeframe: str) -> Path:
    return STRUCTURE_DIR / symbol.upper() / f"{timeframe}.latest.json"


def store_market_structure_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    append_jsonl(structure_path(str(bundle.get("symbol") or ""), str(bundle.get("timeframe") or "")), bundle)
    write_json_atomic(structure_latest_path(str(bundle.get("symbol") or ""), str(bundle.get("timeframe") or "")), bundle)
    return bundle
