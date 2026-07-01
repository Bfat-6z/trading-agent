"""Phase 3 — historical closed-kline fetcher for backtesting (paper-only research).

Pulls a long history (months) of CLOSED Binance USDT-M klines by paging on
startTime, normalizes to the same bar schema as chart_candle_service, and caches
to a deterministic local file so backtests replay identically. Read-only market
data (futures_klines); never places orders.

No-lookahead by construction: only bars whose close_time <= fetch cutoff and
is_final=True are kept. The cache stores raw normalized bars; the backtest driver
applies the point-in-time cutoff per decision.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from atomic_state import read_json, write_json_atomic
from chart_candle_service import normalize_binance_kline
from timebase import utc_now

ROOT = Path(__file__).resolve().parent
BACKTEST_DATA_DIR = ROOT / "state" / "backtest" / "candles"

_TF_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
_MAX_LIMIT = 1000


def _cache_path(symbol: str, timeframe: str) -> Path:
    return BACKTEST_DATA_DIR / symbol.upper() / f"{timeframe}.json"


def fetch_history(
    symbol: str,
    timeframe: str,
    *,
    months: float = 9.0,
    end_ms: int,
    client: Any,
    finality_latency_seconds: int = 0,
    sleep_between: float = 0.0,
) -> list[dict[str, Any]]:
    """Page CLOSED klines back `months` from end_ms. Returns normalized bars
    sorted by open_time, deduped, only is_final=True. end_ms MUST be passed in
    (no wall-clock reads here, for deterministic/replayable runs)."""
    symbol = symbol.upper()
    tf_ms = _TF_MS[timeframe]
    span_ms = int(months * 30 * 24 * 3600 * 1000)
    start_ms = end_ms - span_ms
    server_time_iso = _iso_ms(end_ms)
    seen: dict[str, dict[str, Any]] = {}
    cursor = start_ms
    guard = 0
    while cursor < end_ms and guard < 5000:
        guard += 1
        rows = client.futures_klines(symbol=symbol, interval=timeframe, startTime=cursor, limit=_MAX_LIMIT)
        if not rows:
            break
        for raw in rows:
            bar, errs = normalize_binance_kline(
                raw, timeframe,
                server_time=server_time_iso,
                ingested_at=server_time_iso,
                price_basis="last_trade",
                native_timeframe=True,
                finality_latency_seconds=finality_latency_seconds,
            )
            if bar is None or bar.get("is_final") is not True:
                continue
            seen[str(bar["open_time"])] = bar
        last_open = rows[-1][0]
        nxt = last_open + tf_ms
        if nxt <= cursor:
            break
        cursor = nxt
        if len(rows) < _MAX_LIMIT:
            break
        if sleep_between:
            time.sleep(sleep_between)
    return sorted(seen.values(), key=lambda b: b.get("open_time") or "")


def _iso_ms(ms: int) -> str:
    import datetime
    return datetime.datetime.fromtimestamp(ms / 1000, datetime.timezone.utc).isoformat(timespec="seconds")


def build_and_cache(
    symbol: str,
    timeframe: str,
    *,
    months: float,
    end_ms: int,
    client: Any | None = None,
    sleep_between: float = 0.0,
) -> dict[str, Any]:
    """Fetch history and persist to a deterministic cache file. Returns a summary."""
    if client is None:
        from tradingagents.binance.client import spot_client
        client = spot_client()
    bars = fetch_history(symbol, timeframe, months=months, end_ms=end_ms, client=client, sleep_between=sleep_between)
    path = _cache_path(symbol, timeframe)
    payload = {
        "symbol": symbol.upper(),
        "timeframe": timeframe,
        "end_ms": end_ms,
        "months": months,
        "bar_count": len(bars),
        "first_open": bars[0]["open_time"] if bars else None,
        "last_close": bars[-1]["close_time"] if bars else None,
        "fetched_at": utc_now(),
        "bars": bars,
    }
    write_json_atomic(path, payload)
    return {k: v for k, v in payload.items() if k != "bars"}


def load_cached_history(symbol: str, timeframe: str) -> list[dict[str, Any]]:
    payload = read_json(_cache_path(symbol, timeframe), default={})
    return payload.get("bars", []) if isinstance(payload, dict) else []


def gap_report(bars: list[dict[str, Any]], timeframe: str) -> dict[str, Any]:
    """Report missing-bar gaps (data quality) so the backtest can flag holes."""
    from timebase import parse_utc
    tf_ms = _TF_MS[timeframe]
    gaps = 0
    max_gap = 0
    prev = None
    for b in bars:
        ot = parse_utc(b.get("open_time"))
        if ot is None:
            continue
        cur = int(ot.timestamp() * 1000)
        if prev is not None:
            delta = cur - prev
            if delta > tf_ms:
                missing = delta // tf_ms - 1
                gaps += int(missing)
                max_gap = max(max_gap, int(missing))
        prev = cur
    return {"bar_count": len(bars), "missing_bars": gaps, "largest_gap_bars": max_gap}
