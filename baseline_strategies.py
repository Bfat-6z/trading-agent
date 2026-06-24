"""Simple deterministic baselines for evaluation."""
from __future__ import annotations

from typing import Any


def no_trade_baseline(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return []


def momentum_baseline(candles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    signals = []
    for i in range(1, len(candles) - 1):
        prev = float(candles[i - 1]["close"])
        cur = float(candles[i]["close"])
        if cur > prev:
            signals.append({"index": i + 1, "side": "LONG", "reason": "close_up"})
        elif cur < prev:
            signals.append({"index": i + 1, "side": "SHORT", "reason": "close_down"})
    return signals


def mean_reversion_baseline(candles: list[dict[str, Any]], threshold: float = 0.01) -> list[dict[str, Any]]:
    signals = []
    for i in range(1, len(candles) - 1):
        prev = float(candles[i - 1]["close"])
        cur = float(candles[i]["close"])
        move = (cur - prev) / prev if prev else 0.0
        if move > threshold:
            signals.append({"index": i + 1, "side": "SHORT", "reason": "overextended_up"})
        elif move < -threshold:
            signals.append({"index": i + 1, "side": "LONG", "reason": "overextended_down"})
    return signals
