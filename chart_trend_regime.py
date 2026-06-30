"""MA ribbon and trend-regime classification for chart intelligence."""
from __future__ import annotations

import hashlib
from typing import Any

from agent_data_contracts import CHART_MODEL_VERSION, SCHEMA_VERSION
from atomic_state import canonical_json
from timebase import utc_now

COMPRESSION_PCT = 0.005
OVEREXTENDED_ATR_MULTIPLE = 3.0
SLOPE_LOOKBACK = 5


def stable_digest(prefix: str, payload: Any) -> str:
    return f"{prefix}_" + hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()[:20]


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except Exception:
        return default


def last_series_value(bundle: dict[str, Any], name: str, default: float = 0.0) -> float:
    series = bundle.get("series") if isinstance(bundle.get("series"), dict) else {}
    values = series.get(name) if isinstance(series.get(name), list) else []
    if not values:
        return default
    return safe_float(values[-1], default)


def series_slope(bundle: dict[str, Any], name: str, lookback: int = SLOPE_LOOKBACK) -> float:
    series = bundle.get("series") if isinstance(bundle.get("series"), dict) else {}
    values = [safe_float(value, 0.0) for value in (series.get(name) if isinstance(series.get(name), list) else []) if value is not None]
    if len(values) <= lookback:
        return 0.0
    base = values[-lookback - 1]
    if base == 0:
        return 0.0
    return round((values[-1] - base) / abs(base), 8)


def classify_timeframe_trend(indicator_bundle: dict[str, Any]) -> dict[str, Any]:
    indicators = indicator_bundle.get("indicators") if isinstance(indicator_bundle.get("indicators"), dict) else {}
    ema = indicators.get("ema") if isinstance(indicators.get("ema"), dict) else {}
    close = last_series_value(indicator_bundle, "close", safe_float(indicators.get("current_price")))
    ema20 = safe_float(ema.get("20"))
    ema50 = safe_float(ema.get("50"))
    ema200 = safe_float(ema.get("200"))
    atr14 = safe_float(indicators.get("atr14"))
    slope20 = series_slope(indicator_bundle, "ema20")
    slope50 = series_slope(indicator_bundle, "ema50")
    reason_codes: list[str] = []
    blockers: list[str] = []
    if close <= 0 or ema20 <= 0 or ema50 <= 0:
        bias = "neutral"
        blockers.append("insufficient_ema_data")
    elif ema200 > 0 and ema20 > ema50 > ema200 and slope20 > 0:
        bias = "bullish"
        reason_codes.extend(["ema_ribbon_bull", "trend_aligned"])
    elif ema200 > 0 and ema20 < ema50 < ema200 and slope20 < 0:
        bias = "bearish"
        reason_codes.extend(["ema_ribbon_bear", "trend_aligned"])
    elif ema20 > ema50 and slope20 > 0:
        bias = "bullish"
        reason_codes.append("ema_ribbon_bull")
    elif ema20 < ema50 and slope20 < 0:
        bias = "bearish"
        reason_codes.append("ema_ribbon_bear")
    else:
        bias = "neutral"
        blockers.append("ribbon_flat")
    ema_values = [value for value in (ema20, ema50, ema200) if value > 0]
    spread_pct = (max(ema_values) - min(ema_values)) / close if close > 0 and ema_values else 0.0
    ribbon_state = "compressed" if spread_pct < COMPRESSION_PCT else "expanded"
    if ribbon_state == "compressed":
        blockers.append("ribbon_flat")
    distance_ema20_pct = (close - ema20) / close if close else 0.0
    atr_multiple = abs(close - ema20) / atr14 if atr14 > 0 else 0.0
    overextended = atr_multiple >= OVEREXTENDED_ATR_MULTIPLE
    if overextended:
        blockers.append("too_far_from_ema")
        reason_codes.append("overextended")
    confidence = 0.35
    if bias in {"bullish", "bearish"}:
        confidence += 0.3
    if ema200 > 0 and "trend_aligned" in reason_codes:
        confidence += 0.2
    if ribbon_state == "compressed":
        confidence -= 0.15
    if overextended:
        confidence -= 0.2
    if indicator_bundle.get("degradation_state") != "ok":
        confidence -= 0.15
    confidence = max(0.0, min(1.0, confidence))
    payload = {
        "schema_version": SCHEMA_VERSION,
        "chart_model_version": CHART_MODEL_VERSION,
        "contract": "ChartTrendRegime.v1",
        "trend_regime_id": stable_digest(
            "chart_trend",
            {
                "indicator_id": indicator_bundle.get("indicator_id"),
                "bias": bias,
                "ema20": ema20,
                "ema50": ema50,
                "ema200": ema200,
                "close": close,
            },
        ),
        "symbol": indicator_bundle.get("symbol"),
        "timeframe": indicator_bundle.get("timeframe"),
        "bias": bias,
        "confidence": round(confidence, 4),
        "ema": {"20": ema20, "50": ema50, "200": ema200},
        "close": close,
        "slope": {"ema20": slope20, "ema50": slope50},
        "distance": {"price_vs_ema20_pct": round(distance_ema20_pct, 8), "atr_multiple_from_ema20": round(atr_multiple, 6)},
        "ribbon": {"state": ribbon_state, "spread_pct": round(spread_pct, 8)},
        "overextended": overextended,
        "reason_codes": sorted(set(reason_codes)),
        "blockers": sorted(set(blockers)),
        "source_indicator_id": indicator_bundle.get("indicator_id"),
        "source_ids": indicator_bundle.get("source_ids") or [],
        "input_event_ids": indicator_bundle.get("input_event_ids") or [],
        "decision_cutoff": indicator_bundle.get("decision_cutoff"),
        "created_at": utc_now(),
        "can_place_live_orders": False,
        "live_permission": False,
    }
    return payload


def aggregate_trend_regime(regimes_by_timeframe: dict[str, dict[str, Any]]) -> dict[str, Any]:
    rows = {tf: row for tf, row in regimes_by_timeframe.items() if isinstance(row, dict)}
    counts = {"bullish": 0, "bearish": 0, "neutral": 0}
    weighted = {"bullish": 0.0, "bearish": 0.0, "neutral": 0.0}
    weights = {"1D": 3.0, "4h": 2.0, "1h": 1.5, "15m": 1.0, "5m": 0.75, "1m": 0.5}
    blockers: list[str] = []
    for timeframe, row in rows.items():
        bias = row.get("bias") if row.get("bias") in counts else "neutral"
        weight = weights.get(timeframe, 1.0)
        counts[bias] += 1
        weighted[bias] += weight * safe_float(row.get("confidence"), 0.0)
        blockers.extend(row.get("blockers") if isinstance(row.get("blockers"), list) else [])
    top_bias = max(weighted, key=lambda key: weighted[key]) if rows else "neutral"
    nonzero = [bias for bias, value in weighted.items() if value > 0.0]
    mixed = len([bias for bias in nonzero if bias != "neutral"]) > 1
    if mixed:
        blockers.append("mixed_timeframes")
    total_weight = sum(weighted.values())
    agreement_score = weighted[top_bias] / total_weight if total_weight > 0 else 0.0
    if agreement_score < 0.55:
        top_bias = "neutral"
        blockers.append("mixed_timeframes")
    return {
        "schema_version": SCHEMA_VERSION,
        "chart_model_version": CHART_MODEL_VERSION,
        "contract": "ChartTrendRegimeAggregate.v1",
        "aggregate_id": stable_digest("chart_trend_aggregate", {tf: row.get("trend_regime_id") for tf, row in rows.items()}),
        "bias": top_bias,
        "agreement_score": round(agreement_score, 4),
        "counts": counts,
        "weighted": {key: round(value, 6) for key, value in weighted.items()},
        "blockers": sorted(set(blockers)),
        "timeframes": rows,
        "created_at": utc_now(),
        "can_place_live_orders": False,
        "live_permission": False,
    }
