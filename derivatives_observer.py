"""Deterministic derivatives snapshot evaluator for Phase B."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from agent_data_contracts import SCHEMA_VERSION
from atomic_state import write_json_atomic
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
STATE_DIR = ROOT / "state"
DERIVATIVES_LATEST = STATE_DIR / "derivatives_latest.json"


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except Exception:
        return default


def evaluate_derivatives(
    symbol: str,
    funding_rate: Any = None,
    oi_now: Any = None,
    oi_prev: Any = None,
    long_short_ratio: Any = None,
    taker_buy_sell_ratio: Any = None,
    *,
    mark_price: Any = None,
    next_funding_time: Any = None,
    funding_interval_hours: Any = 8,
    updated_at: str | None = None,
) -> dict[str, Any]:
    funding = safe_float(funding_rate)
    oi_n = safe_float(oi_now)
    oi_p = safe_float(oi_prev)
    mark = safe_float(mark_price)
    oi_delta = (oi_n - oi_p) / oi_p if oi_p else 0.0
    ls = safe_float(long_short_ratio, 1.0)
    taker = safe_float(taker_buy_sell_ratio, 1.0)
    flags = []
    if abs(funding) > 0.001:
        flags.append("funding_extreme")
    if abs(oi_delta) > 0.05:
        flags.append("open_interest_expansion")
    if ls > 2.0 or ls < 0.5:
        flags.append("crowded_positioning")
    if taker > 1.5 or taker < 0.67:
        flags.append("taker_flow_imbalance")
    confidence = 0.25 + 0.15 * sum(value is not None for value in (funding_rate, oi_now, oi_prev, long_short_ratio, taker_buy_sell_ratio))
    payload = {
        "schema_version": SCHEMA_VERSION,
        "updated_at": updated_at or utc_now(),
        "symbol": symbol.upper(),
        "venue": "binance_usdm",
        "price_basis": "MARK",
        "funding_rate": funding,
        "predicted_funding_rate": funding,
        "settled_funding_rate": None,
        "backfilled_settled_rate": False,
        "rate_decimal": funding,
        "payer_side": "LONG" if funding > 0 else "SHORT" if funding < 0 else "NONE",
        "next_funding_time": next_funding_time,
        "funding_interval_hours": safe_float(funding_interval_hours, 8.0),
        "open_interest_delta": round(oi_delta, 8),
        "open_interest_raw": oi_n,
        "open_interest_unit": "contract_or_base_qty",
        "open_interest_base_qty": oi_n,
        "open_interest_quote_notional": round(oi_n * mark, 8) if mark > 0 else None,
        "open_interest_usd_notional": round(oi_n * mark, 8) if mark > 0 else None,
        "long_short_ratio": ls,
        "taker_buy_sell_ratio": taker,
        "flags": flags,
        "confidence": round(min(1.0, confidence), 4),
        "status": "ok" if confidence >= 0.55 else "degraded_missing_derivatives",
    }
    write_json_atomic(DERIVATIVES_LATEST, payload)
    return payload
