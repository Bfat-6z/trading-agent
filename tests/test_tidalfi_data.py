"""Tests for tidalfi_data — the TidalFi 42-market OHLCV adapter (not wired yet).

Run: cd E:\\keo-moi-mail\\trading-agent && venv\\Scripts\\python.exe -m pytest tests\\test_tidalfi_data.py -v

Live-API tests hit https://td.tidalfi.ai (public, no auth); they skip cleanly if
the venue is unreachable so CI/offline runs stay green.
"""
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pytest

import tidalfi_data as td

# ---------------------------------------------------------------------------
# the mission bar contract (orderflow_data.fetch_klines_with_flow producer shape)
# ---------------------------------------------------------------------------
CONTRACT_KEYS = {"open_time", "close_time", "ts_ms", "open", "high", "low", "close",
                 "volume", "quote_volume", "taker_buy_base", "taker_buy_quote", "is_final"}


def _live_bars(symbol="NVDAUSDT", tf="15m", limit=300):
    bars = td.fetch_klines(symbol, tf, limit=limit)
    if not bars:
        pytest.skip("TidalFi API unreachable (fail-open returned []) — live smoke skipped")
    return bars


# ---------------------------------------------------------------------------
# pure-function tests (no network)
# ---------------------------------------------------------------------------
def test_tf_mapping():
    assert td._TF_RES["15m"] == "15"
    assert td._TF_RES["1h"] == "60"
    assert td._TF_RES["4h"] == "240"
    assert td._TF_RES["1d"] == "1D"
    # tf-ms table must agree with the mission's (build_context uses of._TF_MS[TF])
    import orderflow_data as of
    for k, v in td._TF_MS.items():
        if k in of._TF_MS:
            assert v == of._TF_MS[k], f"tf {k} drifted from orderflow_data"


def test_iso_ms_parity_with_orderflow_data():
    """close_time ISO string must be byte-identical to orderflow_data._iso_ms —
    compute_indicators re-parses it into ts_ms and enrich_indicator_df fail-closes
    on any mismatch."""
    import orderflow_data as of
    for ms in (1784112299999, 1784112300000, 0):
        assert td._iso_ms(ms) == of._iso_ms(ms)


def _synthetic_udf(t0=1_784_000_000, n=5, tf_s=900):
    ts = [t0 + i * tf_s for i in range(n)]
    return {"s": "ok", "t": ts,
            "o": [100.0 + i for i in range(n)], "h": [101.0 + i for i in range(n)],
            "l": [99.0 + i for i in range(n)], "c": [100.5 + i for i in range(n)],
            "v": [10.0 * (i + 1) for i in range(n)]}


def test_forming_bar_cutoff_synthetic():
    tf_ms = 900_000
    u = _synthetic_udf(n=5)
    # 'now' lands mid-way through the 5th bar -> it is forming -> exactly 4 closed bars
    now_ms = (u["t"][4] + 450) * 1000
    bars = td._bars_from_udf(u, tf_ms, now_ms)
    assert len(bars) == 4
    assert all(int(b["ts_ms"]) < now_ms for b in bars)          # all truly closed
    # exactly at a boundary: bar 5 closes at t4+900 -> cutoff == that instant keeps it
    bars5 = td._bars_from_udf(u, tf_ms, (u["t"][4] + 900) * 1000)
    assert len(bars5) == 5
    # ts_ms = open + tf - 1 (Binance ...999 convention), and matches close_time ISO
    b = bars[0]
    assert b["ts_ms"] == u["t"][0] * 1000 + tf_ms - 1
    assert b["ts_ms"] % 1000 == 999
    assert b["is_final"] is True
    assert set(b.keys()) == CONTRACT_KEYS


def test_bars_from_udf_bad_payloads():
    assert td._bars_from_udf({"s": "no_data"}, 900_000, 10**15) == []
    assert td._bars_from_udf(None, 900_000, 10**15) == []
    assert td._bars_from_udf({"s": "ok", "t": [], "o": [], "h": [], "l": [], "c": [], "v": []},
                             900_000, 10**15) == []


def test_bars_from_udf_unsorted_and_dupes():
    tf_ms = 900_000
    u = _synthetic_udf(n=4)
    # shuffle + duplicate one open time
    for k in ("t", "o", "h", "l", "c", "v"):
        u[k] = [u[k][2], u[k][0], u[k][2], u[k][1], u[k][3]]
    bars = td._bars_from_udf(u, tf_ms, (u["t"][0] + 10 * 900) * 1000)
    ts = [b["ts_ms"] for b in bars]
    assert ts == sorted(ts) and len(ts) == len(set(ts)) == 4     # chronological, deduped


def test_session_meta_pure_gaps_and_freshness():
    tf_ms = 900_000
    t0 = 1_784_000_000_000
    def bar(close_ms, flat=False):
        px_h = 100.0 if flat else 101.0
        return {"ts_ms": close_ms, "open": 100.0, "high": px_h, "low": 100.0,
                "close": 100.0, "volume": 0.0 if flat else 5.0}
    closes = [t0 + i * tf_ms for i in (0, 1, 2, 5, 6, 10)]       # gaps: 2 missing, 3 missing
    bars = [bar(c) for c in closes[:-1]] + [bar(closes[-1], flat=True)]
    now_ms = closes[-1] + tf_ms                                  # within 2*tf -> fresh
    sm = td.session_meta("NVDAUSDT", bars, "15m", now_ms=now_ms)
    assert sm is not None
    assert sm["gap_count"] == 2
    assert sm["longest_gap_bars"] == 3
    assert sm["fresh"] is True
    assert sm["flat_bar_frac"] == round(1 / 6, 3)
    # stale: last close 3*tf ago
    sm2 = td.session_meta("NVDAUSDT", bars, "15m", now_ms=closes[-1] + 3 * tf_ms)
    assert sm2["fresh"] is False
    # fail-open on junk
    assert td.session_meta("NVDAUSDT", None, "15m") is None      # type: ignore[arg-type]
    assert td.session_meta("NVDAUSDT", [], "bogus_tf") is None


# ---------------------------------------------------------------------------
# live-API smoke (skips if venue unreachable)
# ---------------------------------------------------------------------------
def test_live_nvda_15m_contract():
    now_ms = int(time.time() * 1000)
    bars = _live_bars("NVDAUSDT", "15m", limit=300)
    assert len(bars) >= 50
    assert len(bars) <= 300
    for b in bars:
        assert set(b.keys()) == CONTRACT_KEYS
        assert b["is_final"] is True
    ts = [b["ts_ms"] for b in bars]
    assert ts == sorted(ts) and len(set(ts)) == len(ts)          # chronological, unique
    assert all(t2 - t1 >= 900_000 for t1, t2 in zip(ts, ts[1:]))  # no sub-tf spacing
    # forming bar excluded: every returned bar's close time is in the past
    assert ts[-1] < now_ms
    # ...and the last bar is recent (24/7 venue): closed within 2*tf
    assert now_ms - ts[-1] <= 2 * 900_000
    # neutral taker split (no fabricated CVD)
    b = bars[-1]
    assert b["taker_buy_base"] == pytest.approx(b["volume"] / 2.0)
    assert b["quote_volume"] == pytest.approx(b["volume"] * b["close"])


def test_live_bars_feed_mission_pipeline():
    """The money test: TidalFi bars must flow through the mission's own
    compute_indicators -> enrich_indicator_df (STRICT fail-closed ts_ms join)
    exactly like Binance bars do in build_context (llm_trader.py:376-377)."""
    import backtest_chart_signal as cs
    import orderflow_data as of
    bars = _live_bars("NVDAUSDT", "15m", limit=250)
    ind = cs.compute_indicators(bars)
    assert [int(x) for x in ind["ts_ms"].tolist()] == [b["ts_ms"] for b in bars]
    enr = of.enrich_indicator_df(ind, bars, [])                  # fund=[] per integration plan
    assert len(enr) == len(bars)
    assert float(enr["funding_rate"].iloc[-1]) == 0.0
    assert float(enr["cvd_delta"].iloc[-1]) == 0.0               # neutral, not fabricated


def test_live_session_meta_equity():
    bars = _live_bars("NVDAUSDT", "15m", limit=200)
    sm = td.session_meta("NVDAUSDT", bars, "15m")
    assert sm is not None
    assert sm["kind"] == "equity"
    assert isinstance(sm["fresh"], bool)
    assert sm["gap_count"] >= 0 and sm["longest_gap_bars"] >= 0
    assert 0.0 <= sm["flat_bar_frac"] <= 1.0


def test_live_tidalfi_only_universe():
    syms = td.tidalfi_only_symbols()
    if not syms:
        pytest.skip("TidalFi API unreachable — universe smoke skipped")
    # the TradFi perps are TidalFi-only; the Binance-overlap coins are not
    for s in ("NVDAUSDT", "XAUUSDT", "OPENAIUSDT"):
        assert s in syms
    for s in ("BTCUSDT", "ETHUSDT", "SOLUSDT"):
        assert s not in syms
    assert len(syms) >= 20                                       # 22 as of 2026-07-15


def test_tidalfi_only_fallback_overlap(monkeypatch):
    """When Binance exchangeInfo is unreachable the hardcoded 20-coin overlap
    fallback must still subtract the Binance-listed coins."""
    monkeypatch.setattr(td, "_binance_perp_bases", lambda: None)
    syms = td.tidalfi_only_symbols()
    if not syms:
        pytest.skip("TidalFi API unreachable — fallback smoke skipped")
    assert "BTCUSDT" not in syms and "NVDAUSDT" in syms
