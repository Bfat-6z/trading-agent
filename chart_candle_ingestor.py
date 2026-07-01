"""Phase 1 — real closed-candle ingestor.

chart_candle_service.load_closed_candles() is disk-only; nothing populated its
cache, so the paper decision path fell back to fabricated ticker-proxy candles.
This module fetches REAL closed Binance USDT-M klines for a bounded set of
symbols and persists them so load_closed_candles() has honest data.

Design (Phase 1, edge-first plan 260701):
- One timeframe (5m) to start; expand later.
- Fail-closed per symbol: a fetch error just skips that symbol (its downstream
  candidate will capability-mask 'skip' -> auto-rejected by the brain). Never
  fabricates, never crashes the caller.
- Idempotent: store_candle_batch dedups by open_time downstream; re-ingesting is
  safe. Reads only (futures_klines); does NOT place orders.
- Intended to run inline before scoring (paper_candidate_feeder.run_once) for the
  handful of symbols about to be scored. Can be promoted to a supervised loop
  later without changing this API.
"""
from __future__ import annotations

from typing import Any, Iterable

from chart_candle_service import (
    fetch_binance_futures_candles,
    store_candle_batch,
)

DEFAULT_TIMEFRAME = "5m"
DEFAULT_LIMIT = 200
# Bound how many symbols we fetch per run so an inline call stays cheap.
MAX_SYMBOLS_PER_RUN = 20


def _is_usable_batch(batch: dict[str, Any]) -> bool:
    """A batch is worth persisting only if it carries real final bars and is not
    a provider_error/quarantined stub."""
    if not isinstance(batch, dict):
        return False
    if batch.get("degradation_state") == "quarantined":
        return False
    bars = batch.get("bars") or []
    return any(bar.get("is_final") is True for bar in bars)


def ingest_symbol(symbol: str, timeframe: str = DEFAULT_TIMEFRAME, *, limit: int = DEFAULT_LIMIT, client: Any | None = None) -> dict[str, Any]:
    """Fetch + persist real closed candles for one symbol/timeframe.

    Returns a small result dict; never raises for network/provider issues.
    """
    symbol = str(symbol or "").upper()
    if not symbol:
        return {"symbol": symbol, "stored": False, "reason": "empty_symbol"}
    try:
        batch = fetch_binance_futures_candles(symbol, timeframe, limit=limit, client=client)
    except Exception as exc:  # defensive: fetch already catches, this is belt-and-suspenders
        return {"symbol": symbol, "stored": False, "reason": f"fetch_error:{str(exc)[:80]}"}
    if not _is_usable_batch(batch):
        return {"symbol": symbol, "stored": False, "reason": batch.get("degradation_state") or "no_final_bars"}
    store_candle_batch(batch)
    final_bars = sum(1 for bar in (batch.get("bars") or []) if bar.get("is_final") is True)
    return {"symbol": symbol, "stored": True, "timeframe": timeframe, "final_bars": final_bars}


def ingest_symbols(symbols: Iterable[str], timeframe: str = DEFAULT_TIMEFRAME, *, limit: int = DEFAULT_LIMIT, max_symbols: int = MAX_SYMBOLS_PER_RUN, client: Any | None = None) -> dict[str, Any]:
    """Ingest candles for a bounded, de-duplicated list of symbols.

    Fail-closed and bounded: caps at max_symbols, skips symbols that error.
    """
    seen: list[str] = []
    for sym in symbols:
        up = str(sym or "").upper()
        if up and up not in seen:
            seen.append(up)
        if len(seen) >= max_symbols:
            break
    results = [ingest_symbol(sym, timeframe, limit=limit, client=client) for sym in seen]
    stored = [r for r in results if r.get("stored")]
    return {
        "timeframe": timeframe,
        "requested": len(seen),
        "stored": len(stored),
        "skipped": len(results) - len(stored),
        "results": results,
    }


def symbols_from_market_snapshot(market: dict[str, Any], keys: tuple[str, ...] = ("hot", "top_gainers", "top_losers", "funding_extremes")) -> list[str]:
    """Extract the candidate symbols from a market snapshot, in the same order the
    feeder scans them, so we ingest exactly what will be scored."""
    out: list[str] = []
    for key in keys:
        rows = market.get(key) if isinstance(market.get(key), list) else []
        for row in rows:
            if isinstance(row, dict):
                sym = str(row.get("symbol") or "").upper()
                if sym and sym not in out:
                    out.append(sym)
    return out
