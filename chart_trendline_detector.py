"""Trendline and channel detection from confirmed chart pivots."""
from __future__ import annotations

import hashlib
from pathlib import Path
from statistics import mean
from typing import Any

from agent_data_contracts import CHART_MODEL_VERSION, SCHEMA_VERSION, validate_chart_contract
from atomic_state import append_jsonl, canonical_json, write_json_atomic
from timebase import parse_utc, utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
TRENDLINE_DIR = STATE_DIR / "chart" / "trendlines"

DEFAULT_PERCENT_TOLERANCE = 0.003
DEFAULT_ATR_TOLERANCE_MULTIPLE = 0.20
DEFAULT_MAX_SLOPE_PCT_PER_BAR = 0.06
DEFAULT_PARALLEL_SLOPE_PCT = 0.006
DEFAULT_MAX_PIVOTS_PER_KIND = 18


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
    rows: list[dict[str, Any]] = []
    cutoff_dt = parse_utc(decision_cutoff)
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


def confirmed_pivots(pivot_bundle: dict[str, Any], *, kind: str) -> list[dict[str, Any]]:
    cutoff_dt = parse_utc(pivot_bundle.get("decision_cutoff"))
    structures = pivot_bundle.get("structures") if isinstance(pivot_bundle.get("structures"), dict) else {}
    rows: list[dict[str, Any]] = []
    if not cutoff_dt:
        return rows
    for pivot in structures.get("pivots", []) if isinstance(structures.get("pivots"), list) else []:
        if not isinstance(pivot, dict) or pivot.get("kind") != kind:
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
    rows.sort(key=lambda item: (int(item["x"]), str(item.get("pivot_id") or "")))
    return rows


def tolerance_value(current_price: float, atr: float | None, *, percent_tolerance: float, atr_tolerance_multiple: float) -> float:
    return max(abs(current_price) * percent_tolerance, (atr or 0.0) * atr_tolerance_multiple, 0.0)


def line_value(slope: float, intercept: float, x: int | float) -> float:
    return slope * float(x) + intercept


def relation_for_line(line_type: str, projected: float, previous_projected: float, current_price: float, previous_close: float | None, tolerance: float) -> str:
    if line_type == "support":
        if previous_close is not None and previous_close < previous_projected - tolerance and current_price >= projected - tolerance:
            return "fakeout_reclaim"
        if current_price < projected - tolerance:
            return "losing_line"
        if abs(current_price - projected) <= tolerance:
            return "testing_line"
        return "holding_line"
    if previous_close is not None and previous_close > previous_projected + tolerance and current_price <= projected + tolerance:
        return "fakeout_reject"
    if current_price > projected + tolerance:
        return "breakout"
    if abs(current_price - projected) <= tolerance:
        return "testing_line"
    return "holding_line"


def build_line(
    *,
    symbol: str,
    timeframe: str,
    line_type: str,
    p1: dict[str, Any],
    p2: dict[str, Any],
    pivots: list[dict[str, Any]],
    bars: list[dict[str, Any]],
    current_price: float,
    tolerance: float,
    max_slope_pct_per_bar: float,
) -> dict[str, Any] | None:
    x1 = int(p1["x"])
    x2 = int(p2["x"])
    if x2 <= x1:
        return None
    y1 = float(p1["price"])
    y2 = float(p2["price"])
    slope = (y2 - y1) / (x2 - x1)
    avg_price = max(1e-9, abs((y1 + y2) / 2.0))
    slope_pct_per_bar = slope / avg_price
    if abs(slope_pct_per_bar) > max_slope_pct_per_bar:
        return None
    intercept = y1 - slope * x1
    touches: list[str] = []
    touch_indexes: list[int] = []
    for pivot in pivots:
        x = int(pivot["x"])
        if x < x1:
            continue
        projected = line_value(slope, intercept, x)
        if abs(float(pivot["price"]) - projected) <= tolerance:
            touches.append(str(pivot.get("pivot_id")))
            touch_indexes.append(x)
    current_x = int(bars[-1]["sequence_index"]) if bars else max(x1, x2)
    current_projected = line_value(slope, intercept, current_x)
    prev_close = float(bars[-2]["close"]) if len(bars) >= 2 else None
    prev_projected = line_value(slope, intercept, current_x - 1)
    violations = 0
    checked = 0
    for bar in bars:
        x = int(bar["sequence_index"])
        if x < x1:
            continue
        checked += 1
        projected = line_value(slope, intercept, x)
        close = float(bar["close"])
        if line_type == "support" and close < projected - tolerance:
            violations += 1
        if line_type == "resistance" and close > projected + tolerance:
            violations += 1
    recency_bars = max(0, current_x - max(touch_indexes or [x2]))
    touch_score = min(1.0, len(set(touches)) / 3.0)
    recency_score = max(0.0, min(1.0, 1.0 - recency_bars / 120.0))
    slope_score = max(0.0, min(1.0, 1.0 - abs(slope_pct_per_bar) / max(1e-9, max_slope_pct_per_bar)))
    violation_score = max(0.0, 1.0 - violations / max(1, checked))
    strength = round(max(0.0, min(1.0, 0.34 * touch_score + 0.24 * recency_score + 0.18 * slope_score + 0.24 * violation_score)), 4)
    relation = relation_for_line(line_type, current_projected, prev_projected, current_price, prev_close, tolerance)
    direction = "rising" if slope > tolerance / max(1, x2 - x1) else "falling" if slope < -tolerance / max(1, x2 - x1) else "flat"
    material = {
        "symbol": symbol,
        "timeframe": timeframe,
        "line_type": line_type,
        "pivot_ids": [p1.get("pivot_id"), p2.get("pivot_id")],
        "slope": round(slope, 10),
        "intercept": round(intercept, 10),
    }
    line_id = stable_digest("chart_line", material)
    return {
        "line_id": line_id,
        "line_type": line_type,
        "direction": direction,
        "pivot_ids": [str(p1.get("pivot_id")), str(p2.get("pivot_id"))],
        "touch_pivot_ids": sorted(set(touches)),
        "touch_count": len(set(touches)),
        "violation_count": violations,
        "slope": rounded(slope),
        "intercept": rounded(intercept),
        "slope_pct_per_bar": round(slope_pct_per_bar, 8),
        "start_index": x1,
        "end_index": x2,
        "current_projection": rounded(current_projected),
        "current_distance": rounded(current_price - current_projected),
        "current_relation": relation,
        "strength": strength,
        "score_components": {
            "touch": round(touch_score, 4),
            "recency": round(recency_score, 4),
            "slope": round(slope_score, 4),
            "violation": round(violation_score, 4),
        },
        "overlay": {
            "type": "trendline",
            "points": [
                {"index": x1, "time": p1.get("candle_close_time") or p1.get("close_time"), "price": rounded(y1)},
                {"index": x2, "time": p2.get("candle_close_time") or p2.get("close_time"), "price": rounded(y2)},
                {"index": current_x, "time": bars[-1].get("close_time") if bars else None, "price": rounded(current_projected)},
            ],
        },
        "lookahead_guard": {
            "source": "confirmed_pivots_only",
            "latest_pivot_known_at": max(str(p1.get("confirmed_known_at") or ""), str(p2.get("confirmed_known_at") or "")),
        },
    }


def generate_lines(
    *,
    symbol: str,
    timeframe: str,
    pivots: list[dict[str, Any]],
    line_type: str,
    bars: list[dict[str, Any]],
    current_price: float,
    tolerance: float,
    max_slope_pct_per_bar: float,
    min_span_bars: int,
    max_candidates: int,
) -> list[dict[str, Any]]:
    selected = pivots[-DEFAULT_MAX_PIVOTS_PER_KIND:]
    lines: list[dict[str, Any]] = []
    for i, p1 in enumerate(selected):
        for p2 in selected[i + 1 :]:
            if int(p2["x"]) - int(p1["x"]) < min_span_bars:
                continue
            line = build_line(
                symbol=symbol,
                timeframe=timeframe,
                line_type=line_type,
                p1=p1,
                p2=p2,
                pivots=selected,
                bars=bars,
                current_price=current_price,
                tolerance=tolerance,
                max_slope_pct_per_bar=max_slope_pct_per_bar,
            )
            if line:
                lines.append(line)
    lines.sort(key=lambda row: (-float(row["strength"]), -int(row["touch_count"]), int(row["end_index"]), str(row["line_id"])))
    return lines[: max(0, int(max_candidates))]


def channel_relation(support_value: float, resistance_value: float, current_price: float, tolerance: float) -> str:
    if current_price < support_value - tolerance:
        return "below_channel"
    if current_price > resistance_value + tolerance:
        return "above_channel"
    if abs(current_price - support_value) <= tolerance:
        return "near_support"
    if abs(current_price - resistance_value) <= tolerance:
        return "near_resistance"
    midpoint = (support_value + resistance_value) / 2.0
    if abs(current_price - midpoint) <= tolerance:
        return "mid_channel"
    return "inside_channel"


def generate_channels(lines: list[dict[str, Any]], current_price: float, current_x: int, tolerance: float, *, parallel_slope_pct: float, max_channels: int) -> list[dict[str, Any]]:
    supports = [line for line in lines if line.get("line_type") == "support"]
    resistances = [line for line in lines if line.get("line_type") == "resistance"]
    channels: list[dict[str, Any]] = []
    for support in supports:
        for resistance in resistances:
            slope_diff_pct = abs(float(support["slope_pct_per_bar"]) - float(resistance["slope_pct_per_bar"]))
            if slope_diff_pct > parallel_slope_pct:
                continue
            support_value = line_value(float(support["slope"]), float(support["intercept"]), current_x)
            resistance_value = line_value(float(resistance["slope"]), float(resistance["intercept"]), current_x)
            width = resistance_value - support_value
            if width <= max(tolerance, 0.0):
                continue
            slope_avg = (float(support["slope_pct_per_bar"]) + float(resistance["slope_pct_per_bar"])) / 2.0
            direction = "rising" if slope_avg > 0 else "falling" if slope_avg < 0 else "flat"
            relation = channel_relation(support_value, resistance_value, current_price, tolerance)
            parallel_score = max(0.0, min(1.0, 1.0 - slope_diff_pct / max(1e-9, parallel_slope_pct)))
            strength = round(max(0.0, min(1.0, 0.4 * float(support["strength"]) + 0.4 * float(resistance["strength"]) + 0.2 * parallel_score)), 4)
            material = {
                "support_line_id": support["line_id"],
                "resistance_line_id": resistance["line_id"],
                "width": round(width, 8),
                "current_x": current_x,
            }
            channels.append(
                {
                    "channel_id": stable_digest("chart_channel", material),
                    "support_line_id": support["line_id"],
                    "resistance_line_id": resistance["line_id"],
                    "direction": direction,
                    "width": rounded(width),
                    "width_pct": round(width / current_price, 8) if current_price else None,
                    "parallel_slope_diff_pct": round(slope_diff_pct, 8),
                    "current_relation": relation,
                    "support_projection": rounded(support_value),
                    "resistance_projection": rounded(resistance_value),
                    "strength": strength,
                    "overlay": {
                        "type": "channel",
                        "line_ids": [support["line_id"], resistance["line_id"]],
                    },
                }
            )
    channels.sort(key=lambda row: (-float(row["strength"]), str(row["channel_id"])))
    return channels[: max(0, int(max_channels))]


def compute_trendline_bundle(
    pivot_bundle: dict[str, Any],
    *,
    candle_batch: dict[str, Any] | None = None,
    indicator_bundle: dict[str, Any] | None = None,
    current_price: float | None = None,
    percent_tolerance: float = DEFAULT_PERCENT_TOLERANCE,
    atr_tolerance_multiple: float = DEFAULT_ATR_TOLERANCE_MULTIPLE,
    max_slope_pct_per_bar: float = DEFAULT_MAX_SLOPE_PCT_PER_BAR,
    parallel_slope_pct: float = DEFAULT_PARALLEL_SLOPE_PCT,
    min_span_bars: int = 2,
    max_lines_per_side: int = 8,
    max_channels: int = 6,
) -> dict[str, Any]:
    symbol = str(pivot_bundle.get("symbol") or "").upper()
    timeframe = str(pivot_bundle.get("timeframe") or "")
    decision_cutoff = str(pivot_bundle.get("decision_cutoff") or "")
    errors: list[str] = []
    warnings: list[str] = []
    bars = extract_bars(candle_batch, decision_cutoff=decision_cutoff)
    structures = pivot_bundle.get("structures") if isinstance(pivot_bundle.get("structures"), dict) else {}
    price = current_price if current_price is not None else (float(bars[-1]["close"]) if bars else safe_float(structures.get("current_price")))
    if price is None or price <= 0:
        errors.append("invalid_current_price")
        price = 0.0
    atr = atr_from_indicator(indicator_bundle)
    tolerance = tolerance_value(float(price), atr, percent_tolerance=percent_tolerance, atr_tolerance_multiple=atr_tolerance_multiple)
    lows = confirmed_pivots(pivot_bundle, kind="low")
    highs = confirmed_pivots(pivot_bundle, kind="high")
    if len(lows) < 2:
        warnings.append("insufficient_low_pivots_for_support_line")
    if len(highs) < 2:
        warnings.append("insufficient_high_pivots_for_resistance_line")
    support_lines = generate_lines(
        symbol=symbol,
        timeframe=timeframe,
        pivots=lows,
        line_type="support",
        bars=bars,
        current_price=float(price),
        tolerance=tolerance,
        max_slope_pct_per_bar=max_slope_pct_per_bar,
        min_span_bars=min_span_bars,
        max_candidates=max_lines_per_side,
    )
    resistance_lines = generate_lines(
        symbol=symbol,
        timeframe=timeframe,
        pivots=highs,
        line_type="resistance",
        bars=bars,
        current_price=float(price),
        tolerance=tolerance,
        max_slope_pct_per_bar=max_slope_pct_per_bar,
        min_span_bars=min_span_bars,
        max_candidates=max_lines_per_side,
    )
    lines = support_lines + resistance_lines
    if not lines:
        warnings.append("no_trendlines")
    current_x = int(bars[-1]["sequence_index"]) if bars else int(max([pivot.get("x", 0) for pivot in lows + highs], default=0))
    channels = generate_channels(lines, float(price), current_x, tolerance, parallel_slope_pct=parallel_slope_pct, max_channels=max_channels)
    material = {
        "source_structure_id": pivot_bundle.get("structure_id"),
        "current_price": rounded(price),
        "line_ids": [line["line_id"] for line in sorted(lines, key=lambda line: str(line["line_id"]))],
        "channel_ids": [channel["channel_id"] for channel in sorted(channels, key=lambda channel: str(channel["channel_id"]))],
        "params": {
            "percent_tolerance": percent_tolerance,
            "atr_tolerance_multiple": atr_tolerance_multiple,
            "max_slope_pct_per_bar": max_slope_pct_per_bar,
            "parallel_slope_pct": parallel_slope_pct,
        },
    }
    structure_id = stable_digest("chart_trendlines", material)
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
            "pivots": structures.get("pivots", []),
            "trendlines": lines,
            "trendline_count": len(lines),
            "channels": channels,
            "channel_count": len(channels),
            "current_price": rounded(price),
            "current_index": current_x,
            "trendline_policy": {
                "percent_tolerance": percent_tolerance,
                "atr_tolerance_multiple": atr_tolerance_multiple,
                "effective_tolerance": rounded(tolerance),
                "max_slope_pct_per_bar": max_slope_pct_per_bar,
                "parallel_slope_pct": parallel_slope_pct,
                "min_span_bars": min_span_bars,
                "max_lines_per_side": max_lines_per_side,
                "closed_candles_only": True,
                "source": "confirmed_pivots_only",
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


def trendline_path(symbol: str, timeframe: str) -> Path:
    return TRENDLINE_DIR / symbol.upper() / f"{timeframe}.jsonl"


def trendline_latest_path(symbol: str, timeframe: str) -> Path:
    return TRENDLINE_DIR / symbol.upper() / f"{timeframe}.latest.json"


def store_trendline_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    append_jsonl(trendline_path(str(bundle.get("symbol") or ""), str(bundle.get("timeframe") or "")), bundle)
    write_json_atomic(trendline_latest_path(str(bundle.get("symbol") or ""), str(bundle.get("timeframe") or "")), bundle)
    return bundle
