"""Paper-only chart risk plan from structure, zones, and score evidence."""
from __future__ import annotations

import hashlib
import math
from typing import Any

from agent_data_contracts import CHART_MODEL_VERSION, SCHEMA_VERSION, validate_chart_contract
from atomic_state import canonical_json
from timebase import utc_now

RISK_MODEL_VERSION = "chart_risk_model_v1"
DEFAULT_FEE_PCT = 0.0008
DEFAULT_FUNDING_PCT = 0.0002
DEFAULT_SLIPPAGE_PCT = 0.0005
DEFAULT_MIN_RR = 1.15


def stable_digest(prefix: str, payload: Any) -> str:
    return f"{prefix}_" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:20]


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def rounded(value: Any, digits: int = 10) -> float | None:
    try:
        return round(float(value), digits)
    except Exception:
        return None


def floor_to_tick(price: float, tick_size: float) -> float:
    if tick_size <= 0:
        return price
    return math.floor(price / tick_size) * tick_size


def ceil_to_tick(price: float, tick_size: float) -> float:
    if tick_size <= 0:
        return price
    return math.ceil(price / tick_size) * tick_size


def atr_from_indicator(indicator_bundle: dict[str, Any] | None) -> float:
    indicators = indicator_bundle.get("indicators") if isinstance(indicator_bundle, dict) and isinstance(indicator_bundle.get("indicators"), dict) else {}
    return safe_float(indicators.get("atr14"), 0.0)


def zones_from_bundle(zone_bundle: dict[str, Any] | None) -> dict[str, Any]:
    structures = zone_bundle.get("structures") if isinstance(zone_bundle, dict) and isinstance(zone_bundle.get("structures"), dict) else {}
    return structures.get("nearest") if isinstance(structures.get("nearest"), dict) else {}


def invalidation_from_structure(structure_bundle: dict[str, Any] | None) -> float | None:
    structures = structure_bundle.get("structures") if isinstance(structure_bundle, dict) and isinstance(structure_bundle.get("structures"), dict) else {}
    value = structures.get("invalidation_level")
    if value in (None, ""):
        return None
    return safe_float(value)


def stop_from_structure(side: str, entry: float, atr: float, zone_bundle: dict[str, Any] | None, structure_bundle: dict[str, Any] | None, tick_size: float) -> tuple[float | None, list[str]]:
    nearest = zones_from_bundle(zone_bundle)
    structure_level = invalidation_from_structure(structure_bundle)
    buffer = max(atr * 0.15, entry * 0.001, tick_size)
    reasons: list[str] = []
    if side == "LONG":
        candidates = []
        if structure_level and structure_level < entry:
            candidates.append(structure_level)
            reasons.append("structure_invalidation")
        support = nearest.get("support") if isinstance(nearest.get("support"), dict) else None
        if support and safe_float(support.get("lower")) < entry:
            candidates.append(safe_float(support.get("lower")))
            reasons.append("support_zone")
        if not candidates:
            return None, reasons
        return floor_to_tick(min(candidates) - buffer, tick_size), reasons
    candidates = []
    if structure_level and structure_level > entry:
        candidates.append(structure_level)
        reasons.append("structure_invalidation")
    resistance = nearest.get("resistance") if isinstance(nearest.get("resistance"), dict) else None
    if resistance and safe_float(resistance.get("upper")) > entry:
        candidates.append(safe_float(resistance.get("upper")))
        reasons.append("resistance_zone")
    if not candidates:
        return None, reasons
    return ceil_to_tick(max(candidates) + buffer, tick_size), reasons


def tp_candidates(side: str, entry: float, sl: float, atr: float, zone_bundle: dict[str, Any] | None, min_rr: float) -> list[dict[str, Any]]:
    nearest = zones_from_bundle(zone_bundle)
    risk = abs(entry - sl)
    rows: list[dict[str, Any]] = []
    if side == "LONG":
        resistance = nearest.get("resistance") if isinstance(nearest.get("resistance"), dict) else None
        if resistance and safe_float(resistance.get("lower")) > entry:
            rows.append({"source": "nearest_resistance", "price": safe_float(resistance.get("lower"))})
        rows.append({"source": "rr_min", "price": entry + risk * min_rr})
        if atr > 0:
            rows.append({"source": "atr_extension", "price": entry + atr * 1.5})
    else:
        support = nearest.get("support") if isinstance(nearest.get("support"), dict) else None
        if support and safe_float(support.get("upper")) < entry:
            rows.append({"source": "nearest_support", "price": safe_float(support.get("upper"))})
        rows.append({"source": "rr_min", "price": entry - risk * min_rr})
        if atr > 0:
            rows.append({"source": "atr_extension", "price": entry - atr * 1.5})
    clean: list[dict[str, Any]] = []
    for row in rows:
        price = safe_float(row.get("price"))
        reward = price - entry if side == "LONG" else entry - price
        if reward <= 0 or risk <= 0:
            continue
        rr = reward / risk
        clean.append({"source": row["source"], "price": rounded(price), "rr": round(rr, 4)})
    clean.sort(key=lambda row: (safe_float(row["rr"]), safe_float(row["price"])), reverse=side == "LONG")
    return clean


def leverage_hint(entry: float, sl: float, instrument: dict[str, Any], portfolio_context: dict[str, Any] | None, *, liquidation_reference: float, side: str) -> dict[str, Any]:
    max_leverage = max(1.0, safe_float(instrument.get("max_leverage"), 20.0))
    maintenance_margin_rate = safe_float(instrument.get("maintenance_margin_rate"), 0.005)
    stop_pct = abs(entry - sl) / entry if entry > 0 else 1.0
    raw = 50.0 if stop_pct <= 0.006 else 25.0 if stop_pct <= 0.012 else 10.0 if stop_pct <= 0.025 else 3.0
    hint = min(raw, max_leverage)
    blockers: list[str] = []
    while hint > 1:
        liq_distance_pct = max(0.0, (1.0 / hint) - maintenance_margin_rate)
        liq_price = liquidation_reference * (1.0 - liq_distance_pct) if side == "LONG" else liquidation_reference * (1.0 + liq_distance_pct)
        if (side == "LONG" and liq_price < sl) or (side == "SHORT" and liq_price > sl):
            break
        hint -= 1
    if hint < raw:
        blockers.append("liquidation_proximity_reduced_leverage")
    portfolio = portfolio_context or {}
    same_direction = safe_float(portfolio.get("same_direction_exposure_usd"), 0.0)
    cap = safe_float(portfolio.get("same_direction_leverage_cap"), 0.0)
    if same_direction > 0 and cap > 0 and hint > cap:
        hint = cap
        blockers.append("correlated_exposure_cap")
    return {"leverage": round(max(1.0, hint), 2), "blockers": blockers, "stop_distance_pct": round(stop_pct, 8)}


def compute_chart_risk_plan(
    *,
    symbol: str,
    side: str,
    entry_reference: float,
    chart_score: dict[str, Any],
    zone_bundle: dict[str, Any] | None = None,
    structure_bundle: dict[str, Any] | None = None,
    indicator_bundle: dict[str, Any] | None = None,
    instrument: dict[str, Any] | None = None,
    portfolio_context: dict[str, Any] | None = None,
    mark_price: float | None = None,
    index_price: float | None = None,
    min_rr: float = DEFAULT_MIN_RR,
    fee_pct: float = DEFAULT_FEE_PCT,
    funding_pct: float = DEFAULT_FUNDING_PCT,
    slippage_pct: float = DEFAULT_SLIPPAGE_PCT,
) -> dict[str, Any]:
    side = side.upper()
    entry = safe_float(entry_reference)
    instrument = instrument or {}
    tick_size = safe_float(instrument.get("tick_size"), 0.01)
    step_size = safe_float(instrument.get("step_size"), 0.001)
    min_notional = safe_float(instrument.get("min_notional"), 5.0)
    atr = atr_from_indicator(indicator_bundle)
    errors: list[str] = []
    warnings: list[str] = []
    if side not in {"LONG", "SHORT"}:
        errors.append("invalid_side")
    if entry <= 0:
        errors.append("invalid_entry_reference")
    sl, invalidation_reasons = stop_from_structure(side, entry, atr, zone_bundle, structure_bundle, tick_size)
    if sl is None:
        errors.append("no_valid_invalidation")
        sl = 0.0
    if side == "LONG" and sl >= entry:
        errors.append("invalid_long_sl")
    if side == "SHORT" and sl <= entry:
        errors.append("invalid_short_sl")
    tp_ladder = tp_candidates(side, entry, sl, atr, zone_bundle, min_rr)
    round_tp = []
    for row in tp_ladder[:3]:
        price = safe_float(row["price"])
        price = floor_to_tick(price, tick_size) if side == "LONG" else ceil_to_tick(price, tick_size)
        rr = abs(price - entry) / abs(entry - sl) if abs(entry - sl) > 0 else 0.0
        round_tp.append({**row, "price": rounded(price), "rr": round(rr, 4)})
    cost_pct = fee_pct + funding_pct + slippage_pct
    valid_tp = [row for row in round_tp if row.get("source") != "rr_min" and safe_float(row.get("rr")) >= min_rr and abs(safe_float(row.get("price")) - entry) / entry > cost_pct]
    if not valid_tp:
        errors.append("no_valid_tp_rr_after_costs")
    mark = safe_float(mark_price, entry)
    index = safe_float(index_price, mark)
    if mark <= 0 or index <= 0:
        errors.append("missing_mark_or_index_price")
    lev = leverage_hint(entry, sl, instrument, portfolio_context, liquidation_reference=mark, side=side)
    warnings.extend(lev["blockers"])
    if lev["leverage"] <= 1 and "liquidation_proximity_reduced_leverage" in lev["blockers"]:
        errors.append("liquidation_proximity_blocks_high_leverage")
    qty_step_example = max(step_size, min_notional / max(entry, 1.0))
    rr_primary = valid_tp[0]["rr"] if valid_tp else 0.0
    source_ids = list(chart_score.get("source_ids") or ["chart_setup_scorer"])
    input_event_ids = list(chart_score.get("input_event_ids") or [])
    material = {"symbol": symbol.upper(), "side": side, "entry": entry, "sl": sl, "tp_ladder": valid_tp, "leverage": lev["leverage"], "chart_score_id": chart_score.get("score_id")}
    risk_plan_id = stable_digest("chart_risk_plan", material)
    degradation_state = "quarantined" if errors or chart_score.get("degradation_state") == "quarantined" else "partial" if warnings else "ok"
    payload = {
        "schema_version": SCHEMA_VERSION,
        "chart_model_version": CHART_MODEL_VERSION,
        "contract": "ChartRiskPlan.v1",
        "risk_plan_id": risk_plan_id,
        "symbol": symbol.upper(),
        "side": side,
        "entry_reference": rounded(entry),
        "invalidation": {"level": rounded(sl), "source": invalidation_reasons, "buffered": True},
        "sl": rounded(sl),
        "tp_ladder": valid_tp,
        "rr": round(rr_primary, 4),
        "risk_hint": {"leverage_hint": lev["leverage"], "stop_distance_pct": lev["stop_distance_pct"], "notional_minimum": min_notional, "qty_step_example": rounded(qty_step_example, 8), "final_size_authority": "portfolio_risk_engine"},
        "cost_assumptions": {"fee_pct": fee_pct, "funding_pct": funding_pct, "slippage_pct": slippage_pct, "total_pct": cost_pct},
        "exchange_filters": {"tick_size": tick_size, "step_size": step_size, "min_notional": min_notional, "max_leverage": safe_float(instrument.get("max_leverage"), 20.0), "leverage_bracket": instrument.get("leverage_bracket"), "maintenance_margin_rate": safe_float(instrument.get("maintenance_margin_rate"), 0.005)},
        "price_basis_refs": {"entry": "last_trade", "mark_price": rounded(mark), "index_price": rounded(index), "liquidation_reference": "mark_price"},
        "portfolio_caps": portfolio_context or {},
        "source_ids": source_ids,
        "input_event_ids": input_event_ids,
        "decision_cutoff": chart_score.get("decision_cutoff") or utc_now(),
        "cutoff_proof": chart_score.get("cutoff_proof") or {"ok": False, "errors": ["missing_cutoff_proof"]},
        "degradation_state": degradation_state,
        "capability_mask": {
            "action": "skip" if errors else "size_cap" if warnings else "normal",
            "value_errors": sorted(set(errors)),
            "warnings": sorted(set(warnings)),
            "source_confidence": 0.0 if errors else 0.6 if warnings else 1.0,
        },
        "risk_model_version": RISK_MODEL_VERSION,
        "created_at": utc_now(),
        "can_place_live_orders": False,
        "live_permission": False,
    }
    validation = validate_chart_contract("ChartRiskPlan.v1", payload)
    if not validation.ok:
        payload["degradation_state"] = "quarantined"
        payload["capability_mask"]["action"] = "skip"
        payload["capability_mask"]["value_errors"] = sorted(set(payload["capability_mask"]["value_errors"] + validation.errors))
    return payload
