"""Versioned market regime labels from deterministic feature payloads."""
from __future__ import annotations

from typing import Any

REGIME_VERSION = "regime_v1"


def label_regime(features: dict[str, Any]) -> dict[str, Any]:
    trend = float(features.get("trend_strength") or 0.0)
    atr_pct = float(features.get("atr_pct") or 0.0)
    volume_ratio = float(features.get("volume_ratio") or 0.0)
    derivatives_confidence = float(features.get("derivatives_confidence") or 0.0)
    if trend > 0.01:
        direction = "uptrend"
    elif trend < -0.01:
        direction = "downtrend"
    else:
        direction = "range"
    volatility = "high_vol" if atr_pct >= 0.02 else "low_vol" if atr_pct <= 0.006 else "normal_vol"
    participation = "high_participation" if volume_ratio >= 1.5 else "thin_participation" if volume_ratio <= 0.7 else "normal_participation"
    confidence = min(1.0, max(0.0, 0.55 + min(abs(trend) * 10, 0.25) + (0.1 if volume_ratio > 0 else 0.0) + derivatives_confidence * 0.1))
    return {
        "regime_version": REGIME_VERSION,
        "direction": direction,
        "volatility": volatility,
        "participation": participation,
        "label": f"{direction}:{volatility}:{participation}",
        "confidence": round(confidence, 4),
    }
