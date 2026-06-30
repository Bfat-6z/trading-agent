"""Liquidity, sweep, VWAP, and volume context for chart intelligence."""
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
LIQUIDITY_DIR = STATE_DIR / "chart" / "liquidity"

DEFAULT_PERCENT_TOLERANCE = 0.002
DEFAULT_ATR_TOLERANCE_MULTIPLE = 0.20
DEFAULT_VOLUME_CONFIRMATION_RATIO = 1.5


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


def extract_bars(candle_batch: dict[str, Any], *, decision_cutoff: str | None) -> list[dict[str, Any]]:
    cutoff_dt = parse_utc(decision_cutoff)
    rows: list[dict[str, Any]] = []
    if not cutoff_dt:
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
        volume = safe_float(bar.get("volume"), 0.0)
        if None in (high, low, close):
            continue
        rows.append({"open_time": bar.get("open_time"), "close_time": bar.get("close_time"), "high": float(high), "low": float(low), "close": float(close), "volume": float(volume or 0.0)})
    rows.sort(key=lambda row: str(row.get("open_time") or ""))
    for idx, row in enumerate(rows):
        row["sequence_index"] = idx
    return rows


def indicator_value(indicator_bundle: dict[str, Any] | None, name: str) -> Any:
    indicators = indicator_bundle.get("indicators") if isinstance(indicator_bundle, dict) and isinstance(indicator_bundle.get("indicators"), dict) else {}
    return indicators.get(name)


def atr_from_indicator(indicator_bundle: dict[str, Any] | None) -> float | None:
    return safe_float(indicator_value(indicator_bundle, "atr14"))


def volume_context(indicator_bundle: dict[str, Any] | None, *, threshold: float) -> dict[str, Any]:
    ratio = indicator_value(indicator_bundle, "volume_ratio")
    if not isinstance(ratio, dict) or ratio.get("status") != "ok":
        return {"status": "missing_volume", "value": None, "confirmed": False, "threshold": threshold}
    value = safe_float(ratio.get("value"))
    return {"status": "ok", "value": rounded(value, 6), "confirmed": bool(value is not None and value >= threshold), "threshold": threshold}


def vwap_context(indicator_bundle: dict[str, Any] | None, current_price: float) -> dict[str, Any]:
    vwap = indicator_value(indicator_bundle, "vwap")
    if not isinstance(vwap, dict) or vwap.get("status") != "ok":
        return {"status": "missing_vwap", "value": None, "relation": "unknown", "distance_pct": None}
    value = safe_float(vwap.get("value"))
    if value is None or value <= 0:
        return {"status": "missing_vwap", "value": None, "relation": "unknown", "distance_pct": None}
    relation = "above_vwap" if current_price > value else "below_vwap" if current_price < value else "at_vwap"
    return {"status": "ok", "value": rounded(value), "relation": relation, "distance_pct": round((current_price - value) / current_price, 8) if current_price else None, "session_start_utc": vwap.get("session_start_utc")}


def tolerance_value(current_price: float, atr: float | None, *, percent_tolerance: float, atr_tolerance_multiple: float) -> float:
    return max(abs(current_price) * percent_tolerance, (atr or 0.0) * atr_tolerance_multiple, 0.0)


def cluster_levels(rows: list[dict[str, Any]], key: str, tolerance: float) -> list[dict[str, Any]]:
    values = sorted([(float(row[key]), int(row["sequence_index"]), str(row.get("close_time") or "")) for row in rows], key=lambda item: item[0])
    clusters: list[list[tuple[float, int, str]]] = []
    for item in values:
        if not clusters:
            clusters.append([item])
            continue
        center = mean(value for value, _, _ in clusters[-1])
        if abs(item[0] - center) <= tolerance:
            clusters[-1].append(item)
        else:
            clusters.append([item])
    zones: list[dict[str, Any]] = []
    for cluster in clusters:
        if len(cluster) < 2:
            continue
        prices = [value for value, _, _ in cluster]
        indexes = [idx for _, idx, _ in cluster]
        level = mean(prices)
        side = "buy_side" if key == "high" else "sell_side"
        material = {"side": side, "level": round(level, 8), "indexes": indexes}
        zones.append(
            {
                "liquidity_zone_id": stable_digest("chart_liquidity_zone", material),
                "side": side,
                "kind": "equal_highs" if key == "high" else "equal_lows",
                "level": rounded(level),
                "lower": rounded(min(prices) - tolerance * 0.25),
                "upper": rounded(max(prices) + tolerance * 0.25),
                "touch_count": len(cluster),
                "source_indexes": indexes,
                "last_touch_time": max(ts for _, _, ts in cluster),
            }
        )
    zones.sort(key=lambda zone: (-int(zone["touch_count"]), str(zone["liquidity_zone_id"])))
    return zones


def zone_levels_from_structure(zone_bundle: dict[str, Any] | None) -> list[dict[str, Any]]:
    structures = zone_bundle.get("structures") if isinstance(zone_bundle, dict) and isinstance(zone_bundle.get("structures"), dict) else {}
    rows: list[dict[str, Any]] = []
    for zone in structures.get("zones", []) if isinstance(structures.get("zones"), list) else []:
        if not isinstance(zone, dict) or zone.get("invalidation", {}).get("state") != "active":
            continue
        rows.append({"zone_id": zone.get("zone_id"), "zone_type": zone.get("zone_type"), "lower": safe_float(zone.get("lower")), "upper": safe_float(zone.get("upper"))})
    return rows


def detect_sweeps_and_breakouts(
    bars: list[dict[str, Any]],
    *,
    zones: list[dict[str, Any]],
    liquidity_zones: list[dict[str, Any]],
    tolerance: float,
    volume: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    if not bars:
        return [], [], []
    last = bars[-1]
    high = float(last["high"])
    low = float(last["low"])
    close = float(last["close"])
    events: list[dict[str, Any]] = []
    reason_codes: list[str] = []
    blockers: list[str] = []
    refs = []
    for zone in zones:
        if zone.get("zone_type") == "resistance" and zone.get("upper") is not None:
            refs.append(("resistance", zone.get("zone_id"), float(zone["upper"])))
        if zone.get("zone_type") == "support" and zone.get("lower") is not None:
            refs.append(("support", zone.get("zone_id"), float(zone["lower"])))
    for zone in liquidity_zones:
        refs.append(("buy_side" if zone.get("side") == "buy_side" else "sell_side", zone.get("liquidity_zone_id"), float(zone["level"])))
    for ref_type, ref_id, level in refs:
        if ref_type in {"resistance", "buy_side"}:
            if high > level + tolerance and close <= level + tolerance:
                events.append({"event_type": "BEARISH_LIQUIDITY_SWEEP", "side": "bearish", "reference_id": ref_id, "level": rounded(level), "wick": rounded(high), "close": rounded(close)})
                reason_codes.append("liquidity_sweep_up")
                blockers.append("liquidity_sweep_up")
            elif close > level + tolerance:
                status = "confirmed_breakout" if volume.get("confirmed") else "weak_breakout_no_volume"
                events.append({"event_type": "BREAKOUT_UP", "side": "bullish", "reference_id": ref_id, "level": rounded(level), "close": rounded(close), "volume_status": status})
                if volume.get("confirmed"):
                    reason_codes.append("volume_confirmed")
                else:
                    blockers.append("weak_breakout_no_volume")
        if ref_type in {"support", "sell_side"}:
            if low < level - tolerance and close >= level - tolerance:
                events.append({"event_type": "BULLISH_LIQUIDITY_SWEEP", "side": "bullish", "reference_id": ref_id, "level": rounded(level), "wick": rounded(low), "close": rounded(close)})
                reason_codes.append("liquidity_sweep_down")
                blockers.append("liquidity_sweep_down")
            elif close < level - tolerance:
                status = "confirmed_breakdown" if volume.get("confirmed") else "weak_breakdown_no_volume"
                events.append({"event_type": "BREAKDOWN_DOWN", "side": "bearish", "reference_id": ref_id, "level": rounded(level), "close": rounded(close), "volume_status": status})
                if volume.get("confirmed"):
                    reason_codes.append("volume_confirmed")
                else:
                    blockers.append("weak_breakout_no_volume")
    return events, sorted(set(reason_codes)), sorted(set(blockers))


def divergence_flags(indicator_bundle: dict[str, Any] | None) -> list[dict[str, Any]]:
    series = indicator_bundle.get("series") if isinstance(indicator_bundle, dict) and isinstance(indicator_bundle.get("series"), dict) else {}
    closes = [safe_float(value) for value in series.get("close", []) if safe_float(value) is not None]
    rsi = [safe_float(value) for value in series.get("rsi14", []) if safe_float(value) is not None]
    flags: list[dict[str, Any]] = []
    if len(closes) >= 6 and len(rsi) >= 6:
        if closes[-1] > closes[-6] and rsi[-1] < rsi[-6]:
            flags.append({"kind": "bearish_rsi_divergence", "confidence": "weak_context_only"})
        if closes[-1] < closes[-6] and rsi[-1] > rsi[-6]:
            flags.append({"kind": "bullish_rsi_divergence", "confidence": "weak_context_only"})
    return flags


def optional_context_state(optional_context: dict[str, Any] | None) -> tuple[list[str], float]:
    warnings: list[str] = []
    confidence_cap = 1.0
    if not optional_context:
        return warnings, confidence_cap
    for key, value in optional_context.items():
        if isinstance(value, dict) and str(value.get("status") or "").lower() in {"stale", "missing", "error"}:
            warnings.append(f"optional_{key}_{value.get('status')}")
            confidence_cap = min(confidence_cap, 0.65)
    return warnings, confidence_cap


def compute_liquidity_bundle(
    candle_batch: dict[str, Any],
    *,
    indicator_bundle: dict[str, Any] | None = None,
    zone_bundle: dict[str, Any] | None = None,
    optional_context: dict[str, Any] | None = None,
    percent_tolerance: float = DEFAULT_PERCENT_TOLERANCE,
    atr_tolerance_multiple: float = DEFAULT_ATR_TOLERANCE_MULTIPLE,
    volume_confirmation_ratio: float = DEFAULT_VOLUME_CONFIRMATION_RATIO,
) -> dict[str, Any]:
    symbol = str(candle_batch.get("symbol") or "").upper()
    timeframe = str(candle_batch.get("timeframe") or "")
    decision_cutoff = str(candle_batch.get("decision_cutoff") or "")
    errors: list[str] = []
    warnings: list[str] = []
    bars = extract_bars(candle_batch, decision_cutoff=decision_cutoff)
    if not bars:
        errors.append("no_closed_candles")
    current_price = float(bars[-1]["close"]) if bars else 0.0
    atr = atr_from_indicator(indicator_bundle)
    tolerance = tolerance_value(current_price, atr, percent_tolerance=percent_tolerance, atr_tolerance_multiple=atr_tolerance_multiple)
    buy_side = cluster_levels(bars[:-1], "high", tolerance)
    sell_side = cluster_levels(bars[:-1], "low", tolerance)
    volume = volume_context(indicator_bundle, threshold=volume_confirmation_ratio)
    if volume["status"] != "ok":
        warnings.append("volume_missing")
    vwap = vwap_context(indicator_bundle, current_price)
    if vwap["status"] != "ok":
        warnings.append("vwap_missing")
    optional_warnings, confidence_cap = optional_context_state(optional_context)
    warnings.extend(optional_warnings)
    structure_zones = zone_levels_from_structure(zone_bundle)
    events, reason_codes, blockers = detect_sweeps_and_breakouts(bars, zones=structure_zones, liquidity_zones=buy_side + sell_side, tolerance=tolerance, volume=volume)
    divergences = divergence_flags(indicator_bundle)
    if divergences:
        warnings.append("divergence_weak_context_only")
    confidence = 0.35
    if buy_side or sell_side:
        confidence += 0.15
    if events:
        confidence += 0.25
    if volume.get("confirmed"):
        confidence += 0.15
    confidence = round(min(confidence, confidence_cap), 4)
    material = {
        "batch_id": candle_batch.get("batch_id"),
        "indicator_id": indicator_bundle.get("indicator_id") if isinstance(indicator_bundle, dict) else None,
        "zone_structure_id": zone_bundle.get("structure_id") if isinstance(zone_bundle, dict) else None,
        "buy_ids": [row["liquidity_zone_id"] for row in buy_side],
        "sell_ids": [row["liquidity_zone_id"] for row in sell_side],
        "events": events,
        "current_price": rounded(current_price),
    }
    liquidity_id = stable_digest("chart_liquidity", material)
    source_ids = list(candle_batch.get("source_ids") or ["chart_candle_batch"])
    input_event_ids = list(candle_batch.get("input_event_ids") or [])
    if candle_batch.get("batch_id"):
        input_event_ids.append(str(candle_batch["batch_id"]))
    inherited_state = candle_batch.get("degradation_state")
    degradation_state = "quarantined" if errors or inherited_state == "quarantined" else "partial" if warnings or inherited_state == "partial" else "ok"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "chart_model_version": CHART_MODEL_VERSION,
        "contract": "ChartLiquidityBundle.v1",
        "liquidity_id": liquidity_id,
        "symbol": symbol,
        "timeframe": timeframe,
        "price_basis": candle_batch.get("price_basis"),
        "native_timeframe": bool(candle_batch.get("native_timeframe", True)),
        "source_ids": source_ids,
        "input_event_ids": sorted(set(input_event_ids)),
        "decision_cutoff": candle_batch.get("decision_cutoff"),
        "cutoff_proof": candle_batch.get("cutoff_proof") or {"ok": False, "errors": ["missing_cutoff_proof"]},
        "degradation_state": degradation_state,
        "liquidity": {
            "buy_side": buy_side,
            "sell_side": sell_side,
            "events": events,
            "reason_codes": sorted(set(reason_codes)),
            "blockers": blockers,
            "volume": volume,
            "vwap": vwap,
            "divergence": divergences,
            "optional_context": optional_context or {},
            "confidence": confidence,
            "current_price": rounded(current_price),
            "liquidity_policy": {
                "percent_tolerance": percent_tolerance,
                "atr_tolerance_multiple": atr_tolerance_multiple,
                "effective_tolerance": rounded(tolerance),
                "volume_confirmation_ratio": volume_confirmation_ratio,
                "divergence_standalone_entry_allowed": False,
                "closed_candles_only": True,
            },
        },
        "capability_mask": {
            "action": "normal" if degradation_state == "ok" else "size_cap" if degradation_state == "partial" else "skip",
            "value_errors": sorted(set(errors)),
            "warnings": sorted(set(warnings)),
            "source_confidence": confidence if degradation_state != "quarantined" else 0.0,
        },
        "created_at": utc_now(),
        "can_place_live_orders": False,
        "live_permission": False,
    }
    validation = validate_chart_contract("ChartLiquidityBundle.v1", payload)
    if not validation.ok:
        payload["degradation_state"] = "quarantined"
        payload["capability_mask"]["action"] = "skip"
        payload["capability_mask"]["value_errors"] = sorted(set(payload["capability_mask"]["value_errors"] + validation.errors))
    return payload


def liquidity_path(symbol: str, timeframe: str) -> Path:
    return LIQUIDITY_DIR / symbol.upper() / f"{timeframe}.jsonl"


def liquidity_latest_path(symbol: str, timeframe: str) -> Path:
    return LIQUIDITY_DIR / symbol.upper() / f"{timeframe}.latest.json"


def store_liquidity_bundle(bundle: dict[str, Any]) -> dict[str, Any]:
    append_jsonl(liquidity_path(str(bundle.get("symbol") or ""), str(bundle.get("timeframe") or "")), bundle)
    write_json_atomic(liquidity_latest_path(str(bundle.get("symbol") or ""), str(bundle.get("timeframe") or "")), bundle)
    return bundle
