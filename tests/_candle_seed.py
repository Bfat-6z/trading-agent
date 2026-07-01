"""Test helper: seed the real closed-candle cache with valid OHLCV bars.

Phase 1 wired the decision path to real closed candles
(chart_candle_service.load_closed_candles), replacing the fabricated
ticker-proxy. Tests that exercise build_candidates/run_once must therefore seed
a cache so a candidate can form; otherwise the candidate is (correctly) skipped
for lack of honest data.

seed_candles builds finalized bars strictly BEFORE `cutoff` (no lookahead) and
stores them via chart_candle_service into a monkeypatched CHART_CANDLE_DIR.
"""
from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import chart_candle_service as ccs
from timebase import parse_utc


def seed_candles(
    monkeypatch,
    tmp_path: Path,
    symbol: str,
    cutoff_ts: str,
    *,
    timeframe: str = "5m",
    count: int = 12,
    base_price: float = 100.0,
    ingested_after_cutoff: bool = False,
) -> None:
    """Point the candle cache at tmp_path and write `count` valid final bars
    that all finalize before `cutoff_ts`.

    ingested_after_cutoff=True stamps ingested_at LATER than the cutoff (like the
    real ingestor, which writes at fetch time ~now). This must NOT exclude bars —
    ingested_at is operational, not a lookahead signal (Phase 1 M1 fix)."""
    monkeypatch.setattr(ccs, "CHART_CANDLE_DIR", tmp_path / "chart" / "candles")
    cutoff = parse_utc(cutoff_ts)
    assert cutoff is not None, f"invalid cutoff_ts: {cutoff_ts}"
    step = ccs.TIMEFRAME_SECONDS.get(timeframe, 300)
    ingested_stamp = (cutoff + timedelta(seconds=3600)).isoformat(timespec="seconds") if ingested_after_cutoff else None

    bars: list[dict[str, Any]] = []
    # Oldest first; the most recent bar closes one full step before cutoff.
    for i in range(count, 0, -1):
        open_dt = cutoff - timedelta(seconds=step * (i + 1))
        close_dt = cutoff - timedelta(seconds=step * i)
        o = base_price + i * 0.1
        c = base_price + (i - 1) * 0.1
        hi = max(o, c) + 0.5
        lo = min(o, c) - 0.5
        iso_close = close_dt.isoformat(timespec="seconds")
        bars.append({
            "open_time": open_dt.isoformat(timespec="seconds"),
            "close_time": iso_close,
            "open": f"{o:.4f}",
            "high": f"{hi:.4f}",
            "low": f"{lo:.4f}",
            "close": f"{c:.4f}",
            "volume": "1000",
            "quote_volume": "100000",
            "trade_count": 50,
            "is_final": True,
            "available_at": iso_close,
            "known_at": iso_close,
            "ingested_at": ingested_stamp or iso_close,
            "finalized_at": iso_close,
            "price_basis": "last_trade",
            "native_timeframe": True,
        })
    batch = ccs.build_chart_candle_batch(
        symbol,
        timeframe,
        bars,
        decision_cutoff=cutoff_ts,
        source_id="chart_candle_cache",
        provider="local_cache",
        price_basis="last_trade",
        server_time=cutoff_ts,
        ingested_at=cutoff_ts,
        native_timeframe=True,
        min_candles=1,
    )
    ccs.store_candle_batch(batch)
