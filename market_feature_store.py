"""Canonical feature store for paper learning and setup scoring."""
from __future__ import annotations

import hashlib
from pathlib import Path
from statistics import mean
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import write_json_atomic
from regime_labeler import label_regime
from source_provenance import build_provenance
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
FEATURE_STORE_DIR = STATE_DIR / "feature_store"
REGIME_LATEST = STATE_DIR / "agent_memory" / "regime_latest.json"

FEATURE_VERSION = "market_features_v1"


def ema(values: list[float], span: int) -> float:
    if not values:
        return 0.0
    alpha = 2 / (span + 1)
    value = values[0]
    for item in values[1:]:
        value = alpha * item + (1 - alpha) * value
    return value


def true_ranges(candles: list[dict[str, Any]]) -> list[float]:
    ranges: list[float] = []
    prev_close: float | None = None
    for row in candles:
        high = float(row["high"])
        low = float(row["low"])
        close = float(row["close"])
        if prev_close is None:
            ranges.append(high - low)
        else:
            ranges.append(max(high - low, abs(high - prev_close), abs(low - prev_close)))
        prev_close = close
    return ranges


def rsi(values: list[float], period: int = 14) -> float:
    if len(values) < 2:
        return 50.0
    diffs = [values[i] - values[i - 1] for i in range(1, len(values))][-period:]
    gains = [max(0.0, item) for item in diffs]
    losses = [abs(min(0.0, item)) for item in diffs]
    avg_gain = mean(gains) if gains else 0.0
    avg_loss = mean(losses) if losses else 0.0
    if avg_loss == 0:
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def feature_id(symbol: str, timeframe: str, candles: list[dict[str, Any]], derivatives: dict[str, Any] | None) -> str:
    first = candles[0].get("ts") if candles else "empty"
    last = candles[-1].get("ts") if candles else "empty"
    raw = f"{FEATURE_VERSION}:{symbol.upper()}:{timeframe}:{first}:{last}:{len(candles)}:{bool(derivatives)}"
    return "features_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def compute_market_features(
    symbol: str,
    timeframe: str,
    candles: list[dict[str, Any]],
    derivatives: dict[str, Any] | None = None,
    source_ids: list[str] | None = None,
) -> dict[str, Any]:
    if len(candles) < 3:
        raise ValueError("insufficient_candles_for_features")
    closes = [float(row["close"]) for row in candles]
    volumes = [float(row.get("volume", 0.0) or 0.0) for row in candles]
    trs = true_ranges(candles)
    last_close = closes[-1]
    ema_fast = ema(closes, min(5, len(closes)))
    ema_slow = ema(closes, min(20, len(closes)))
    atr = mean(trs[-min(14, len(trs)) :]) if trs else 0.0
    volume_base = mean(volumes[:-1]) if len(volumes) > 1 else volumes[-1]
    derivatives_confidence = 0.0 if not derivatives else float(derivatives.get("confidence", 0.5))
    missing = [] if derivatives else ["derivatives"]
    features = {
        "schema_version": SCHEMA_VERSION,
        "feature_version": FEATURE_VERSION,
        "feature_id": feature_id(symbol, timeframe, candles, derivatives),
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        "window_start": candles[0].get("ts"),
        "window_end": candles[-1].get("ts"),
        "candle_count": len(candles),
        "last_close": round(last_close, 10),
        "ema_fast": round(ema_fast, 10),
        "ema_slow": round(ema_slow, 10),
        "trend_strength": round((ema_fast - ema_slow) / last_close if last_close else 0.0, 8),
        "atr": round(atr, 10),
        "atr_pct": round(atr / last_close if last_close else 0.0, 8),
        "range_pct": round((float(candles[-1]["high"]) - float(candles[-1]["low"])) / last_close if last_close else 0.0, 8),
        "volume_ratio": round(volumes[-1] / volume_base, 6) if volume_base else 0.0,
        "rsi": round(rsi(closes), 4),
        "derivatives_confidence": round(derivatives_confidence, 4),
        "missing_features": missing,
        "feature_confidence": round(max(0.0, min(1.0, 0.85 - 0.25 * len(missing) + derivatives_confidence * 0.1)), 4),
        "computed_at": utc_now(),
    }
    regime = label_regime(features)
    provenance = build_provenance("market_features", source_ids or ["local_state"], input_ids=[features["feature_id"]], metadata={"symbol": symbol.upper()})
    payload = {**features, "regime": regime, "provenance_id": provenance["provenance_id"], "source_ids": provenance["source_ids"]}
    FEATURE_STORE_DIR.mkdir(parents=True, exist_ok=True)
    write_json_atomic(FEATURE_STORE_DIR / f"{payload['feature_id']}.json", payload)
    write_json_atomic(REGIME_LATEST, {"schema_version": SCHEMA_VERSION, "updated_at": utc_now(), "latest_feature_id": payload["feature_id"], "regime": regime})
    return payload
