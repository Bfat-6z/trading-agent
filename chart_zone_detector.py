"""Support/resistance zone detection from confirmed pivots."""
from __future__ import annotations

import hashlib
from pathlib import Path
from statistics import mean, median
from typing import Any

from agent_data_contracts import CHART_MODEL_VERSION, SCHEMA_VERSION, validate_chart_contract
from atomic_state import append_jsonl, canonical_json, write_json_atomic
from timebase import parse_utc, utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
ZONE_DIR = STATE_DIR / "chart" / "zones"

TIMEFRAME_WEIGHTS = {"1D": 3.0, "4h": 2.0, "1h": 1.5, "15m": 1.0, "5m": 0.75, "1m": 0.5}
DEFAULT_PERCENT_TOLERANCE = 0.0025
DEFAULT_ATR_TOLERANCE_MULTIPLE = 0.25
DEFAULT_MAX_WIDTH_PCT = 0.01
DEFAULT_DECAY_BARS = 120


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

def extract_bars(candle_batch: dict[str, Any] | None, *, decision_cutoff: str | None = None) -> list[dict[str, float | str]]:
    rows: list[dict[str, float | str]] = []
    if not candle_batch:
        return rows
    cutoff_dt = parse_utc(decision_cutoff)
    for idx, bar in enumerate(candle_batch.get("bars") or []):
        if not isinstance(bar, dict) or bar.get("is_final") is not True:
            continue
        if cutoff_dt:
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
                "source_index": idx,
                "open_time": str(bar.get("open_time") or ""),
                "close_time": str(bar.get("close_time") or ""),
                "high": float(high),
                "low": float(low),
                "close": float(close),
            }
        )
    rows.sort(key=lambda row: str(row.get("open_time") or ""))
    return rows


def current_price_from_inputs(pivot_bundle: dict[str, Any], bars: list[dict[str, float | str]], current_price: float | None) -> float | None:
    if current_price is not None:
        return float(current_price)
    if bars:
        return float(bars[-1]["close"])
    structures = pivot_bundle.get("structures") if isinstance(pivot_bundle.get("structures"), dict) else {}
    return safe_float(structures.get("current_price"))


def atr_from_indicator(indicator_bundle: dict[str, Any] | None) -> float | None:
    if not indicator_bundle:
        return None
    indicators = indicator_bundle.get("indicators") if isinstance(indicator_bundle.get("indicators"), dict) else {}
    return safe_float(indicators.get("atr14"))


def tolerance_value(
    reference_price: float,
    atr: float | None,
    *,
    percent_tolerance: float,
    atr_tolerance_multiple: float,
) -> float:
    by_percent = abs(reference_price) * percent_tolerance
    by_atr = (atr or 0.0) * atr_tolerance_multiple
    return max(by_percent, by_atr, 0.0)


def sorted_pivots(pivot_bundle: dict[str, Any], kind: str) -> list[dict[str, Any]]:
    structures = pivot_bundle.get("structures") if isinstance(pivot_bundle.get("structures"), dict) else {}
    rows = [row for row in structures.get("pivots", []) if isinstance(row, dict) and row.get("kind") == kind and safe_float(row.get("price")) is not None]
    return sorted(rows, key=lambda row: float(row["price"]))


def cluster_pivots(pivots: list[dict[str, Any]], tolerance: float) -> list[list[dict[str, Any]]]:
    clusters: list[list[dict[str, Any]]] = []
    for pivot in pivots:
        price = float(pivot["price"])
        if not clusters:
            clusters.append([pivot])
            continue
        center = mean(float(item["price"]) for item in clusters[-1])
        if abs(price - center) <= tolerance:
            clusters[-1].append(pivot)
        else:
            clusters.append([pivot])
    return clusters


def latest_pivot_index(pivots: list[dict[str, Any]]) -> int:
    indexes = [int(pivot.get("source_index", pivot.get("sequence_index", 0)) or 0) for pivot in pivots]
    return max(indexes) if indexes else 0


def latest_known_at(pivots: list[dict[str, Any]]) -> str | None:
    parsed = [dt for dt in (parse_utc(pivot.get("known_at")) for pivot in pivots) if dt is not None]
    if not parsed:
        return None
    return max(parsed).isoformat(timespec="seconds")


def relation_for_zone(
    zone_type: str,
    lower: float,
    upper: float,
    current_price: float,
    bars: list[dict[str, float | str]],
    buffer: float,
) -> str:
    close = current_price
    prev_close = float(bars[-2]["close"]) if len(bars) >= 2 else close
    last_high = float(bars[-1]["high"]) if bars else close
    last_low = float(bars[-1]["low"]) if bars else close
    if lower <= close <= upper:
        return "inside_zone"
    if zone_type == "resistance":
        if prev_close <= upper and close > upper + buffer:
            return "breakout_up"
        if prev_close > upper + buffer and last_low <= upper and close > upper + buffer:
            return "retest_hold"
        if last_high >= lower and close < lower:
            return "rejection"
        if close > upper + buffer:
            return "above_broken_resistance"
        return "below_resistance"
    if prev_close >= lower and close < lower - buffer:
        return "breakout_down"
    if prev_close < lower - buffer and last_high >= lower and close < lower - buffer:
        return "retest_reject"
    if last_low <= upper and close > upper:
        return "rejection"
    if close < lower - buffer:
        return "below_broken_support"
    return "above_support"


def build_zone(
    *,
    symbol: str,
    timeframe: str,
    zone_type: str,
    pivots: list[dict[str, Any]],
    tolerance: float,
    max_width: float,
    current_price: float,
    candle_count: int,
    bars: list[dict[str, float | str]],
    atr: float | None,
    decay_bars: int,
) -> dict[str, Any]:
    prices = [float(pivot["price"]) for pivot in pivots]
    lower = min(prices) - tolerance * 0.25
    upper = max(prices) + tolerance * 0.25
    midpoint = (lower + upper) / 2
    width = upper - lower
    width_pct = width / abs(current_price) if current_price else 0.0
    too_wide = bool(max_width > 0 and width > max_width)
    latest_idx = latest_pivot_index(pivots)
    bars_since_latest = max(0, candle_count - 1 - latest_idx)
    touch_count = len(pivots)
    freshness_score = max(0.0, min(1.0, 1.0 - (bars_since_latest / max(1, decay_bars))))
    touch_score = min(1.0, touch_count / 4.0)
    rejection_score = max(0.0, min(1.0, mean(float(pivot.get("rejection_strength") or 0.0) for pivot in pivots)))
    volumes = [float(pivot.get("volume") or 0.0) for pivot in pivots]
    volume_score = 0.0
    if volumes and max(volumes) > 0:
        volume_score = min(1.0, mean(volumes) / max(1.0, median(volumes)))
    timeframe_score = min(1.0, TIMEFRAME_WEIGHTS.get(timeframe, 1.0) / 3.0)
    strength = 0.28 * touch_score + 0.24 * freshness_score + 0.22 * rejection_score + 0.14 * volume_score + 0.12 * timeframe_score
    if too_wide:
        strength *= 0.65
    if touch_count <= 1 and bars_since_latest > decay_bars:
        strength *= 0.35
    strength = round(max(0.0, min(1.0, strength)), 4)
    buffer = max((atr or 0.0) * 0.10, abs(current_price) * 0.001)
    relation = relation_for_zone(zone_type, lower, upper, current_price, bars, buffer)
    invalid = (zone_type == "support" and current_price < lower - buffer) or (zone_type == "resistance" and current_price > upper + buffer)
    decayed = touch_count <= 1 and bars_since_latest > decay_bars
    invalidation_state = "invalidated" if invalid else "decayed" if decayed else "active"
    quality = "strong" if strength >= 0.65 and not too_wide else "fresh" if strength >= 0.45 and not too_wide else "messy" if too_wide else "weak"
    invalidated_at = str(bars[-1].get("close_time")) if invalid and bars else None
    material = {
        "symbol": symbol,
        "timeframe": timeframe,
        "zone_type": zone_type,
        "pivot_ids": [pivot.get("pivot_id") for pivot in pivots],
        "lower": round(lower, 8),
        "upper": round(upper, 8),
    }
    return {
        "zone_id": stable_digest("chart_zone", material),
        "symbol": symbol,
        "timeframe": timeframe,
        "kind": zone_type,
        "zone_type": zone_type,
        "lower": rounded(lower),
        "upper": rounded(upper),
        "mid": rounded(midpoint),
        "midpoint": rounded(midpoint),
        "width_abs": rounded(width),
        "width": rounded(width),
        "width_pct": round(width_pct, 8),
        "width_atr": round(width / atr, 6) if atr and atr > 0 else None,
        "too_wide": too_wide,
        "touch_count": touch_count,
        "constituent_pivot_ids": [str(pivot.get("pivot_id")) for pivot in pivots],
        "first_seen_at": min((str(pivot.get("known_at")) for pivot in pivots if pivot.get("known_at")), default=None),
        "last_touch_at": latest_known_at(pivots),
        "latest_known_at": latest_known_at(pivots),
        "latest_pivot_index": latest_idx,
        "bars_since_latest": bars_since_latest,
        "strength_score": strength,
        "strength": strength,
        "quality": quality,
        "price_relation": relation,
        "active": invalidation_state == "active",
        "state": relation,
        "invalidated_at": invalidated_at,
        "invalidation_reason": "broken_by_current_close" if invalidation_state == "invalidated" else "stale_single_touch" if invalidation_state == "decayed" else None,
        "invalidation": {
            "state": invalidation_state,
            "buffer": rounded(buffer),
            "rule": "support_break_below_buffer" if zone_type == "support" else "resistance_break_above_buffer",
        },
        "score_components": {
            "touch": round(touch_score, 4),
            "freshness": round(freshness_score, 4),
            "rejection": round(rejection_score, 4),
            "volume": round(volume_score, 4),
            "timeframe": round(timeframe_score, 4),
        },
    }


def nearest_zones(zones: list[dict[str, Any]], current_price: float) -> dict[str, Any]:
    active = [zone for zone in zones if zone.get("invalidation", {}).get("state") == "active"]
    supports = [zone for zone in active if zone.get("zone_type") == "support" and safe_float(zone.get("upper"), 0.0) <= current_price]
    resistances = [zone for zone in active if zone.get("zone_type") == "resistance" and safe_float(zone.get("lower"), 0.0) >= current_price]
    nearest_support = min(supports, key=lambda zone: abs(current_price - float(zone["upper"])), default=None)
    nearest_resistance = min(resistances, key=lambda zone: abs(float(zone["lower"]) - current_price), default=None)
    inside = [zone for zone in active if safe_float(zone.get("lower"), 0.0) <= current_price <= safe_float(zone.get("upper"), 0.0)]
    return {
        "support": nearest_support,
        "resistance": nearest_resistance,
        "inside_zone_ids": [zone["zone_id"] for zone in inside],
    }


def current_price_relation(zones: list[dict[str, Any]], current_price: float, atr: float | None) -> dict[str, Any]:
    nearest = nearest_zones(zones, current_price)
    support = nearest.get("support")
    resistance = nearest.get("resistance")
    support_distance = current_price - float(support["upper"]) if support else None
    resistance_distance = float(resistance["lower"]) - current_price if resistance else None
    blockers = []
    inside = nearest.get("inside_zone_ids") or []
    if inside:
        blockers.append("inside_messy_zone")
    relation = "inside_zone" if inside else "between_zones"
    if support and not resistance:
        relation = "above_support"
    if resistance and not support:
        relation = "below_resistance"
    return {
        "reference_price": rounded(current_price),
        "nearest_support_zone_id": support.get("zone_id") if support else None,
        "nearest_resistance_zone_id": resistance.get("zone_id") if resistance else None,
        "inside_zone_ids": inside,
        "relation": relation,
        "distance_to_support_pct": round(support_distance / current_price, 8) if support_distance is not None and current_price else None,
        "distance_to_resistance_pct": round(resistance_distance / current_price, 8) if resistance_distance is not None and current_price else None,
        "distance_to_support_atr": round(support_distance / atr, 6) if support_distance is not None and atr and atr > 0 else None,
        "distance_to_resistance_atr": round(resistance_distance / atr, 6) if resistance_distance is not None and atr and atr > 0 else None,
        "blockers": sorted(set(blockers)),
    }


def zone_blockers_for_entry(zone_bundle: dict[str, Any], *, side: str, setup_score: float, min_setup_score: float = 7.0) -> list[str]:
    structures = zone_bundle.get("structures") if isinstance(zone_bundle.get("structures"), dict) else {}
    zones = structures.get("zones") if isinstance(structures.get("zones"), list) else []
    blockers: list[str] = []
    capability = zone_bundle.get("capability_mask") if isinstance(zone_bundle.get("capability_mask"), dict) else {}
    if zone_bundle.get("degradation_state") == "quarantined" or capability.get("action") == "skip":
        blockers.append("chart_structure_unavailable")
        return blockers
    if zone_bundle.get("can_place_live_orders") is True or zone_bundle.get("live_permission") is True:
        blockers.append("chart_live_permission_violation")
        return blockers
    if setup_score < min_setup_score:
        for zone in zones:
            if zone.get("invalidation", {}).get("state") != "active":
                continue
            if zone.get("price_relation") == "inside_zone":
                blockers.append("inside_messy_zone")
                break
    side = side.upper()
    nearest = structures.get("nearest") if isinstance(structures.get("nearest"), dict) else {}
    if side == "LONG" and nearest.get("resistance") and setup_score < min_setup_score:
        blockers.append("at_resistance")
    if side == "SHORT" and nearest.get("support") and setup_score < min_setup_score:
        blockers.append("at_support")
    return sorted(set(blockers))


def compute_zone_bundle(
    pivot_bundle: dict[str, Any],
    *,
    candle_batch: dict[str, Any] | None = None,
    indicator_bundle: dict[str, Any] | None = None,
    current_price: float | None = None,
    percent_tolerance: float = DEFAULT_PERCENT_TOLERANCE,
    atr_tolerance_multiple: float = DEFAULT_ATR_TOLERANCE_MULTIPLE,
    max_width_pct: float = DEFAULT_MAX_WIDTH_PCT,
    decay_bars: int = DEFAULT_DECAY_BARS,
) -> dict[str, Any]:
    structures = pivot_bundle.get("structures") if isinstance(pivot_bundle.get("structures"), dict) else {}
    symbol = str(pivot_bundle.get("symbol") or "").upper()
    timeframe = str(pivot_bundle.get("timeframe") or "")
    decision_cutoff = str(pivot_bundle.get("decision_cutoff") or "")
    bars = extract_bars(candle_batch, decision_cutoff=decision_cutoff)
    price = current_price_from_inputs(pivot_bundle, bars, current_price)
    errors: list[str] = []
    warnings: list[str] = []
    if price is None or price <= 0:
        errors.append("invalid_current_price")
        price = 0.0
    atr = atr_from_indicator(indicator_bundle)
    reference_prices = [float(pivot["price"]) for kind in ("high", "low") for pivot in sorted_pivots(pivot_bundle, kind)]
    reference_price = price or (median(reference_prices) if reference_prices else 0.0)
    tolerance = tolerance_value(reference_price, atr, percent_tolerance=percent_tolerance, atr_tolerance_multiple=atr_tolerance_multiple)
    if tolerance <= 0:
        errors.append("invalid_zone_tolerance")
    max_width = abs(price) * max_width_pct if price else 0.0
    candle_count = int(structures.get("candle_count") or len(bars) or 0)
    zones: list[dict[str, Any]] = []
    for kind, zone_type in (("low", "support"), ("high", "resistance")):
        for cluster in cluster_pivots(sorted_pivots(pivot_bundle, kind), tolerance):
            zones.append(
                build_zone(
                    symbol=symbol,
                    timeframe=timeframe,
                    zone_type=zone_type,
                    pivots=cluster,
                    tolerance=tolerance,
                    max_width=max_width,
                    current_price=price,
                    candle_count=candle_count,
                    bars=bars,
                    atr=atr,
                    decay_bars=decay_bars,
                )
            )
    if not zones:
        warnings.append("no_support_resistance_zones")
    zones.sort(key=lambda zone: (-float(zone.get("strength") or 0.0), str(zone.get("zone_id") or "")))
    nearest = nearest_zones(zones, price)
    relation = current_price_relation(zones, price, atr)
    material = {
        "source_structure_id": pivot_bundle.get("structure_id"),
        "current_price": rounded(price),
        "zone_ids": [zone["zone_id"] for zone in zones],
        "percent_tolerance": percent_tolerance,
        "atr_tolerance_multiple": atr_tolerance_multiple,
    }
    structure_id = stable_digest("chart_zones", material)
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
            "zones": zones,
            "zone_count": len(zones),
            "current_price": rounded(price),
            "nearest": nearest,
            "current_price_relation": relation,
            "zone_policy": {
                "percent_tolerance": percent_tolerance,
                "atr_tolerance_multiple": atr_tolerance_multiple,
                "effective_tolerance": rounded(tolerance),
                "max_width_pct": max_width_pct,
                "max_width": rounded(max_width),
                "decay_bars": decay_bars,
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


def zone_path(symbol: str, timeframe: str) -> Path:
    return ZONE_DIR / symbol.upper() / f"{timeframe}.jsonl"


def zone_latest_path(symbol: str, timeframe: str) -> Path:
    return ZONE_DIR / symbol.upper() / f"{timeframe}.latest.json"


def store_zone_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    append_jsonl(zone_path(str(bundle.get("symbol") or ""), str(bundle.get("timeframe") or "")), bundle)
    write_json_atomic(zone_latest_path(str(bundle.get("symbol") or ""), str(bundle.get("timeframe") or "")), bundle)
    return bundle
