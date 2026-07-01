"""Phase 1 Piece C — exits resolve on real intrabar OHLC, not single marks."""
from datetime import timedelta
from pathlib import Path

import chart_candle_service as ccs
import paper_execution_lifecycle_loop as loop
from timebase import parse_utc


def _seed_exit_bars(monkeypatch, tmp_path: Path, symbol: str, opened: str, cutoff: str, wick_high: float, at_index: int = 3, count: int = 5):
    monkeypatch.setattr(ccs, "CHART_CANDLE_DIR", tmp_path / "chart" / "candles")
    o = parse_utc(opened)
    step = 300
    bars = []
    for i in range(1, count + 1):
        ct = o + timedelta(seconds=step * i)
        hi = wick_high if i == at_index else 101.0
        iso = ct.isoformat(timespec="seconds")
        bars.append({
            "open_time": (o + timedelta(seconds=step * (i - 1))).isoformat(timespec="seconds"),
            "close_time": iso, "open": "100.5", "high": f"{hi}", "low": "99.5", "close": "100.8",
            "volume": "1000", "is_final": True, "available_at": iso, "known_at": iso,
            "ingested_at": iso, "finalized_at": iso, "price_basis": "last_trade", "native_timeframe": True,
        })
    batch = ccs.build_chart_candle_batch(symbol, "5m", bars, decision_cutoff=cutoff, source_id="chart_candle_cache", provider="local_cache", price_basis="last_trade", server_time=cutoff, ingested_at=cutoff, native_timeframe=True, min_candles=1)
    ccs.store_candle_batch(batch)


def test_intrabar_tp_fires_on_real_wick(monkeypatch, tmp_path):
    opened = "2026-06-21T00:00:00+00:00"
    cutoff = "2026-06-21T00:30:00+00:00"
    _seed_exit_bars(monkeypatch, tmp_path, "BTCUSDT", opened, cutoff, wick_high=103.0)  # > TP 102
    pos = {"symbol": "BTCUSDT", "side": "LONG", "entry": "100", "qty": "1", "sl": "98", "tp": "102", "leverage": "5", "opened_at": opened}
    mark = {"ts": cutoff, "open": 100.8, "high": 100.8, "low": 100.8, "close": 100.8, "volume": 0, "quality": "mark_only_snapshot"}

    bars = loop.real_intrabar_candles(pos, cutoff)
    assert len(bars) >= 3
    assert any(b["high"] >= 102 for b in bars)

    cp = loop.should_close(pos, mark)
    assert cp is not None
    assert cp["reason"] == "tp"  # real intrabar wick, not a blind timeout


def test_no_real_candles_falls_back_to_mark(monkeypatch, tmp_path):
    monkeypatch.setattr(ccs, "CHART_CANDLE_DIR", tmp_path / "empty")
    pos = {"symbol": "BTCUSDT", "side": "LONG", "entry": "100", "qty": "1", "sl": "98", "tp": "102", "leverage": "5", "opened_at": "2026-06-21T00:00:00+00:00"}
    assert loop.real_intrabar_candles(pos, "2026-06-21T00:10:00+00:00") == []
    # a mark that directly touches TP still closes via the fallback single candle
    mark = {"ts": "2026-06-21T00:10:00+00:00", "open": 102.5, "high": 102.5, "low": 102.5, "close": 102.5, "volume": 0, "quality": "mark_only_snapshot"}
    cp = loop.should_close(pos, mark)
    assert cp is not None and cp["reason"] == "tp"


def test_real_bars_before_open_are_excluded_no_lookahead(monkeypatch, tmp_path):
    # bar that wicks TP but closed BEFORE the position opened must be ignored
    opened = "2026-06-21T01:00:00+00:00"
    cutoff = "2026-06-21T01:30:00+00:00"
    # seed bars around 00:00 (all before open at 01:00)
    _seed_exit_bars(monkeypatch, tmp_path, "BTCUSDT", "2026-06-21T00:00:00+00:00", cutoff, wick_high=103.0)
    pos = {"symbol": "BTCUSDT", "side": "LONG", "entry": "100", "qty": "1", "sl": "98", "tp": "102", "leverage": "5", "opened_at": opened}
    bars = loop.real_intrabar_candles(pos, cutoff)
    assert bars == []  # all pre-open bars excluded
