"""Edge-research harness — universe selector (owner directive).

Pick the trading universe by OBJECTIVE liquidity measured at the START of the
backtest window (anti-survivorship), NOT "hot today". For each candidate symbol
we sum quote-volume over the first ~24h of the window; symbols whose start-of-
window daily quote-volume >= threshold qualify, ranked desc, capped at max_symbols.

This avoids survivorship bias: a coin that is liquid today but was illiquid 9
months ago is judged on what it was AT window start, when a trade would have been
placed.
"""
from __future__ import annotations

from typing import Any

import backtest_data_fetcher as bf

# Candidate pool: liquid majors + established alts (objective filter applied on
# top). Kept broad; the volume-at-start threshold does the real selection.
DEFAULT_CANDIDATES = [
    "BTCUSDT", "ETHUSDT", "SOLUSDT", "BNBUSDT", "XRPUSDT", "DOGEUSDT",
    "ADAUSDT", "AVAXUSDT", "LINKUSDT", "LTCUSDT", "DOTUSDT", "MATICUSDT",
    "NEARUSDT", "APTUSDT", "ARBUSDT", "OPUSDT", "INJUSDT", "SUIUSDT",
    "TIAUSDT", "SEIUSDT", "WIFUSDT", "TRXUSDT", "ATOMUSDT", "FILUSDT",
]

# quote-volume over the first day of the window must clear this (USDT)
DEFAULT_MIN_DAILY_QUOTE_VOLUME = 50_000_000.0


def start_window_daily_quote_volume(bars: list[dict[str, Any]], timeframe: str) -> float:
    """Sum quote_volume over the first 24h of the (window-start) bars."""
    if not bars:
        return 0.0
    per_day = {"15m": 96, "1h": 24, "4h": 6, "5m": 288}.get(timeframe, 24)
    head = bars[:per_day]
    total = 0.0
    for b in head:
        qv = b.get("quote_volume")
        if qv is not None:
            try:
                total += float(qv)
            except Exception:
                pass
        else:
            # fallback: close * base volume
            try:
                total += float(b.get("close", 0)) * float(b.get("volume", 0))
            except Exception:
                pass
    return total


def select_universe(client: Any, *, end_ms: int, months: float, timeframe: str = "1h",
                    candidates: list[str] | None = None,
                    min_daily_quote_volume: float = DEFAULT_MIN_DAILY_QUOTE_VOLUME,
                    max_symbols: int = 9, sleep_between: float = 0.02) -> dict[str, Any]:
    """Return {selected: [symbols], detail: {sym: vol}, dropped: {sym: vol}}.
    Liquidity is judged at window start (end_ms - months). Objective + anti-
    survivorship. Fetches a short window (~2 days) per candidate for the measure."""
    candidates = candidates or DEFAULT_CANDIDATES
    start_ms = end_ms - int(months * 30 * 24 * 3600 * 1000)
    measure_end = start_ms + 2 * 24 * 3600 * 1000  # 2 days of bars at window start
    detail: dict[str, float] = {}
    for sym in candidates:
        try:
            bars = bf.fetch_history(sym, timeframe, months=(2 * 24 * 3600 * 1000) / (30 * 24 * 3600 * 1000),
                                    end_ms=measure_end, client=client, sleep_between=sleep_between)
            detail[sym] = start_window_daily_quote_volume(bars, timeframe)
        except Exception:
            detail[sym] = 0.0
    qualified = {s: v for s, v in detail.items() if v >= min_daily_quote_volume}
    ranked = sorted(qualified.items(), key=lambda kv: kv[1], reverse=True)
    selected = [s for s, _ in ranked[:max_symbols]]
    dropped = {s: v for s, v in detail.items() if s not in selected}
    return {"selected": selected, "detail": detail, "dropped": dropped,
            "threshold": min_daily_quote_volume, "measured_at_ms": start_ms}
