"""Minimal screenshot signal metadata validator.

Actual OCR/vision extraction can be added later; this module keeps screenshot
signals from entering as complete when symbol/timeframe is missing.
"""
from __future__ import annotations

from typing import Any


def parse_screenshot_signal(metadata: dict[str, Any]) -> dict[str, Any]:
    symbol = metadata.get("symbol")
    timeframe = metadata.get("timeframe")
    errors = []
    if not symbol:
        errors.append("missing_symbol")
    if not timeframe:
        errors.append("missing_timeframe")
    return {"ok": not errors, "symbol": str(symbol).upper() if symbol else None, "timeframe": timeframe, "errors": errors, "confidence": 0.6 if not errors else 0.0}
