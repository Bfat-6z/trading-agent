"""Deterministic chart setup scoring for paper candidates."""
from __future__ import annotations

import hashlib
from typing import Any

from agent_data_contracts import CHART_MODEL_VERSION, SCHEMA_VERSION, validate_chart_contract
from atomic_state import canonical_json
from timebase import utc_now

SCORING_CONFIG_VERSION = "chart_setup_scorer_v1"
MAX_SCORE = 10.0


def stable_digest(prefix: str, payload: Any) -> str:
    return f"{prefix}_" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:20]


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def side_from_bias(bias: str | None) -> str:
    value = str(bias or "").lower()
    if value in {"bullish", "uptrend", "long"}:
        return "LONG"
    if value in {"bearish", "downtrend", "short"}:
        return "SHORT"
    return "NONE"


def opposite(side: str) -> str:
    return "SHORT" if side == "LONG" else "LONG" if side == "SHORT" else "NONE"


def bundle_bad(bundle: dict[str, Any] | None) -> bool:
    if not isinstance(bundle, dict):
        return True
    capability = bundle.get("capability_mask") if isinstance(bundle.get("capability_mask"), dict) else {}
    return bundle.get("degradation_state") in {"quarantined", "stale"} or capability.get("action") == "skip"


def add_component(components: list[dict[str, Any]], name: str, value: float, reasons: list[str] | None = None) -> None:
    components.append({"name": name, "value": round(value, 4), "reasons": reasons or []})


def zone_evidence(zone_bundle: dict[str, Any] | None, side: str) -> tuple[float, list[str], list[str], list[str]]:
    if not isinstance(zone_bundle, dict):
        return 0.0, [], ["chart_zone_missing"], []
    structures = zone_bundle.get("structures") if isinstance(zone_bundle.get("structures"), dict) else {}
    relation = structures.get("current_price_relation") if isinstance(structures.get("current_price_relation"), dict) else {}
    blockers = [str(item) for item in relation.get("blockers", []) if item]
    reason_codes: list[str] = []
    reasons: list[str] = []
    score = 0.0
    if relation.get("inside_zone_ids"):
        blockers.append("inside_messy_zone")
        reason_codes.append("inside_messy_zone")
    nearest = structures.get("nearest") if isinstance(structures.get("nearest"), dict) else {}
    if side == "LONG" and nearest.get("support"):
        score += 1.0
        reason_codes.append("at_support")
        reasons.append("nearest_support_available")
    if side == "SHORT" and nearest.get("resistance"):
        score += 1.0
        reason_codes.append("at_resistance")
        reasons.append("nearest_resistance_available")
    return score, reason_codes, sorted(set(blockers)), reasons


def liquidity_evidence(liquidity_bundle: dict[str, Any] | None, side: str) -> tuple[float, list[str], list[str], list[str]]:
    if not isinstance(liquidity_bundle, dict):
        return 0.0, [], ["chart_liquidity_missing"], []
    liquidity = liquidity_bundle.get("liquidity") if isinstance(liquidity_bundle.get("liquidity"), dict) else {}
    score = 0.0
    reason_codes = [str(code) for code in liquidity.get("reason_codes", []) if code]
    blockers = [str(code) for code in liquidity.get("blockers", []) if code]
    reasons: list[str] = []
    for event in liquidity.get("events", []) if isinstance(liquidity.get("events"), list) else []:
        event_type = str(event.get("event_type") or "")
        if side == "LONG" and event_type in {"BULLISH_LIQUIDITY_SWEEP", "BREAKOUT_UP"}:
            score += 1.2
            reasons.append(event_type.lower())
        if side == "SHORT" and event_type in {"BEARISH_LIQUIDITY_SWEEP", "BREAKDOWN_DOWN"}:
            score += 1.2
            reasons.append(event_type.lower())
    volume = liquidity.get("volume") if isinstance(liquidity.get("volume"), dict) else {}
    if volume.get("confirmed"):
        score += 0.8
        reason_codes.append("volume_confirmed")
    elif volume.get("status") == "missing_volume":
        reason_codes.append("volume_missing")
    vwap = liquidity.get("vwap") if isinstance(liquidity.get("vwap"), dict) else {}
    if side == "LONG" and vwap.get("relation") == "above_vwap":
        score += 0.3
        reasons.append("above_vwap")
    if side == "SHORT" and vwap.get("relation") == "below_vwap":
        score += 0.3
        reasons.append("below_vwap")
    return min(2.2, score), sorted(set(reason_codes)), sorted(set(blockers)), reasons


def structure_evidence(structure_bundle: dict[str, Any] | None, side: str) -> tuple[float, list[str], list[str], list[str], float | None]:
    if not isinstance(structure_bundle, dict):
        return 0.0, [], ["chart_structure_missing"], [], None
    structures = structure_bundle.get("structures") if isinstance(structure_bundle.get("structures"), dict) else {}
    bias_side = side_from_bias(structures.get("side_bias"))
    score = 0.0
    reasons: list[str] = []
    blockers: list[str] = []
    reason_codes = [str(code) for code in structures.get("reason_codes", []) if code]
    if bias_side == side:
        score += 2.0
        reasons.append("structure_bias_aligned")
    elif bias_side == opposite(side):
        score -= 1.5
        blockers.append("structure_conflict")
    invalidation = structures.get("invalidation_level")
    if invalidation in (None, ""):
        blockers.append("no_sl_level")
        reason_codes.append("no_sl_level")
    return score, sorted(set(reason_codes)), sorted(set(blockers)), reasons, safe_float(invalidation, None)


def trend_evidence(trend_bundle: dict[str, Any] | None, trend_aggregate: dict[str, Any] | None, side: str) -> tuple[float, list[str], list[str], list[str]]:
    score = 0.0
    reasons: list[str] = []
    blockers: list[str] = []
    reason_codes: list[str] = []
    if isinstance(trend_bundle, dict):
        bias_side = side_from_bias(trend_bundle.get("bias"))
        if bias_side == side:
            score += 2.0 * safe_float(trend_bundle.get("confidence"), 0.5)
            reason_codes.extend(trend_bundle.get("reason_codes") or [])
            reason_codes.append("trend_aligned")
            reasons.append("timeframe_trend_aligned")
        elif bias_side == opposite(side):
            score -= 1.5
            blockers.append("trend_conflict")
        if trend_bundle.get("overextended"):
            blockers.append("overextended")
            reason_codes.append("overextended")
    if isinstance(trend_aggregate, dict):
        if "mixed_timeframes" in (trend_aggregate.get("blockers") or []):
            blockers.append("mixed_timeframes")
            reason_codes.append("mixed_timeframes")
            score -= 1.0
        aggregate_side = side_from_bias(trend_aggregate.get("bias"))
        if aggregate_side == opposite(side):
            blockers.append("conflicting_higher_timeframe")
            score -= 1.5
    return score, sorted(set(reason_codes)), sorted(set(blockers)), reasons


def tier_for_score(score: float, blockers: list[str]) -> str:
    critical = {"stale_candles", "no_sl_level", "inside_messy_zone", "mixed_timeframes", "conflicting_higher_timeframe", "overextended", "missing_chart_intelligence_id"}
    if critical.intersection(blockers):
        return "blocked"
    if score >= 9.2:
        return "5A+"
    if score >= 8.0:
        return "A+"
    if score >= 6.5:
        return "B"
    if score >= 5.0:
        return "C"
    return "no_trade"


def score_chart_setup(
    *,
    symbol: str,
    side: str,
    setup_family: str,
    trend_bundle: dict[str, Any] | None = None,
    trend_aggregate: dict[str, Any] | None = None,
    zone_bundle: dict[str, Any] | None = None,
    structure_bundle: dict[str, Any] | None = None,
    liquidity_bundle: dict[str, Any] | None = None,
    require_chart_intelligence_id: bool = False,
    chart_intelligence_id: str | None = None,
) -> dict[str, Any]:
    side = side.upper()
    components: list[dict[str, Any]] = []
    blockers: list[str] = []
    reason_codes: list[str] = []
    evidence_ids: list[str] = []
    source_ids: list[str] = []
    input_event_ids: list[str] = []
    for bundle in (trend_bundle, trend_aggregate, zone_bundle, structure_bundle, liquidity_bundle):
        if not isinstance(bundle, dict):
            continue
        source_ids.extend(str(value) for value in (bundle.get("source_ids") or []) if value)
        input_event_ids.extend(str(value) for value in (bundle.get("input_event_ids") or []) if value)
        for field in ("trend_regime_id", "aggregate_id", "structure_id", "liquidity_id", "indicator_id"):
            if bundle.get(field):
                evidence_ids.append(str(bundle[field]))
    if require_chart_intelligence_id and not chart_intelligence_id:
        blockers.append("missing_chart_intelligence_id")
    for name, bundle in (("zone", zone_bundle), ("structure", structure_bundle), ("liquidity", liquidity_bundle)):
        if bundle_bad(bundle):
            blockers.append("stale_candles")
            reason_codes.append("stale_candles")
            add_component(components, name, -2.0, ["bundle_unavailable"])
    trend_score, trend_codes, trend_blockers, trend_reasons = trend_evidence(trend_bundle, trend_aggregate, side)
    add_component(components, "trend", trend_score, trend_reasons)
    reason_codes.extend(trend_codes)
    blockers.extend(trend_blockers)
    structure_score, structure_codes, structure_blockers, structure_reasons, invalidation = structure_evidence(structure_bundle, side)
    add_component(components, "structure", structure_score, structure_reasons)
    reason_codes.extend(structure_codes)
    blockers.extend(structure_blockers)
    zone_score, zone_codes, zone_blockers, zone_reasons = zone_evidence(zone_bundle, side)
    add_component(components, "zone", zone_score, zone_reasons)
    reason_codes.extend(zone_codes)
    blockers.extend(zone_blockers)
    liquidity_score, liquidity_codes, liquidity_blockers, liquidity_reasons = liquidity_evidence(liquidity_bundle, side)
    add_component(components, "liquidity_volume", liquidity_score, liquidity_reasons)
    reason_codes.extend(liquidity_codes)
    blockers.extend(liquidity_blockers)
    freshness_score = 0.8 if not blockers else 0.2
    add_component(components, "freshness", freshness_score, ["no_critical_data_blockers"] if freshness_score > 0.5 else ["data_blockers_present"])
    raw_score = sum(safe_float(item.get("value")) for item in components)
    score = round(max(0.0, min(MAX_SCORE, raw_score + 2.0)), 4)
    blockers = sorted(set(blockers))
    reason_codes = sorted(set(code for code in reason_codes if code))
    tier = tier_for_score(score, blockers)
    confidence_values = []
    for bundle in (trend_bundle, structure_bundle, liquidity_bundle):
        if isinstance(bundle, dict):
            structures = bundle.get("structures") if isinstance(bundle.get("structures"), dict) else {}
            liquidity = bundle.get("liquidity") if isinstance(bundle.get("liquidity"), dict) else {}
            confidence_values.extend([safe_float(bundle.get("confidence"), -1), safe_float(structures.get("confidence"), -1), safe_float(liquidity.get("confidence"), -1)])
    confidence_values = [value for value in confidence_values if value >= 0]
    confidence = round(sum(confidence_values) / len(confidence_values), 4) if confidence_values else round(score / MAX_SCORE, 4)
    material = {
        "symbol": symbol.upper(),
        "side": side,
        "setup_family": setup_family,
        "components": components,
        "blockers": blockers,
        "reason_codes": reason_codes,
        "evidence_ids": sorted(set(evidence_ids)),
        "chart_intelligence_id": chart_intelligence_id,
    }
    score_id = stable_digest("chart_setup_score", material)
    cutoff = None
    for bundle in (structure_bundle, zone_bundle, liquidity_bundle, trend_bundle):
        if isinstance(bundle, dict) and bundle.get("decision_cutoff"):
            cutoff = bundle.get("decision_cutoff")
            break
    cutoff_proof = None
    for bundle in (structure_bundle, zone_bundle, liquidity_bundle):
        if isinstance(bundle, dict) and isinstance(bundle.get("cutoff_proof"), dict):
            cutoff_proof = bundle.get("cutoff_proof")
            break
    payload = {
        "schema_version": SCHEMA_VERSION,
        "chart_model_version": CHART_MODEL_VERSION,
        "contract": "ChartSetupScore.v1",
        "score_id": score_id,
        "symbol": symbol.upper(),
        "side": side,
        "setup_family": setup_family,
        "score": score,
        "confidence": confidence,
        "tier": tier,
        "components": components,
        "blockers": blockers,
        "reason_codes": reason_codes,
        "evidence_ids": sorted(set(evidence_ids)),
        "chart_intelligence_id": chart_intelligence_id,
        "invalidation_level": invalidation,
        "source_ids": sorted(set(source_ids or ["chart_setup_scorer"])),
        "input_event_ids": sorted(set(input_event_ids)),
        "decision_cutoff": cutoff or utc_now(),
        "cutoff_proof": cutoff_proof or {"ok": False, "errors": ["missing_cutoff_proof"]},
        "degradation_state": "quarantined" if "stale_candles" in blockers else "ok",
        "capability_mask": {
            "action": "skip" if tier == "blocked" or "stale_candles" in blockers else "normal",
            "value_errors": blockers,
            "warnings": [],
            "source_confidence": confidence,
        },
        "scoring_config_version": SCORING_CONFIG_VERSION,
        "created_at": utc_now(),
        "can_place_live_orders": False,
        "live_permission": False,
    }
    validation = validate_chart_contract("ChartSetupScore.v1", payload)
    if not validation.ok:
        payload["degradation_state"] = "quarantined"
        payload["capability_mask"]["action"] = "skip"
        payload["capability_mask"]["value_errors"] = sorted(set(payload["capability_mask"]["value_errors"] + validation.errors))
    return payload


def attach_chart_score_to_candidate(candidate: dict[str, Any], score: dict[str, Any] | None, *, chart_required: bool = False) -> dict[str, Any]:
    updated = dict(candidate)
    if not score:
        if chart_required:
            updated["chart_data_status"] = "missing_required"
            updated["chart_decision_eligible"] = False
            updated["chart_data_capability_mask"] = {"action": "skip", "value_errors": ["missing_chart_score"], "source_confidence": 0.0}
        return updated
    updated["chart_score"] = score
    updated["chart_score_value"] = score.get("score")
    updated["chart_score_tier"] = score.get("tier")
    updated["chart_intelligence_id"] = score.get("chart_intelligence_id") or score.get("score_id")
    updated["chart_data_status"] = "ok" if score.get("degradation_state") == "ok" else score.get("degradation_state")
    updated["chart_decision_eligible"] = score.get("capability_mask", {}).get("action") != "skip"
    updated["chart_data_capability_mask"] = score.get("capability_mask")
    updated["can_place_live_orders"] = False
    return updated
