"""chart_smc — the adapter that revives the owner's SMC detectors. Locks the
contract invariants (the 0-pivot traps) + paper-only guarantee. Uses synthetic
bars (deterministic, offline)."""
from __future__ import annotations

import re
from pathlib import Path

import chart_smc as smc

ROOT = Path(__file__).resolve().parents[1]


def _bars(n=200, start=100.0):
    bars, px = [], start
    t0 = 1_700_000_000_000
    step = 900_000
    for k in range(n):
        o = px
        px = px * (1 + (0.012 if k % 5 < 2 else -0.008))  # deterministic swings -> pivots/zones
        hi, lo = max(o, px) * 1.004, min(o, px) * 0.996
        ct = t0 + (k + 1) * step
        # ISO-8601 strings (what fetch_klines_with_flow emits), not ms ints
        iso = f"2024-01-{1 + (k // 96):02d}T{(k % 96) * 15 // 60:02d}:{(k % 96) * 15 % 60:02d}:00+00:00"
        bars.append({"open_time": iso, "close_time": iso, "open": o, "high": hi,
                     "low": lo, "close": px, "volume": 1000 + (k % 7) * 40})
    return bars


def test_to_candle_batch_contract():
    b = smc.to_candle_batch(_bars(50), "BTCUSDT", "15m")
    assert b["contract"] == "ChartCandleBatch.v1"
    assert b["degradation_state"] == "ok"
    assert b["decision_cutoff"] == b["bars"][-1]["close_time"]
    for bar in b["bars"]:
        # the four finality stamps must equal close_time (the 0-pivot invariant)
        for f in ("known_at", "available_at", "ingested_at", "finalized_at"):
            assert bar[f] == bar["close_time"]
        assert bar["is_final"] is True


def test_smc_summary_runs_or_empty():
    out = smc.smc_summary(_bars(200), "BTCUSDT", "15m")
    # either a real summary (detectors produced structure) or {} — never raises
    assert isinstance(out, dict)
    if out:
        assert "summary" in out and "hlines" in out
        assert isinstance(out["hlines"], list)


def test_short_series_and_bad_tf_return_empty():
    assert smc.smc_summary(_bars(10), "BTCUSDT", "15m") == {}
    assert smc.smc_summary(_bars(200), "BTCUSDT", "3m") == {}   # tf not in contract set


def test_paper_only_no_live_calls():
    text = (ROOT / "chart_smc.py").read_text(encoding="utf-8")
    assert re.compile(r"\.\s*(futures_create_order|create_order|transfer)\s*\(").search(text) is None
    assert "ALLOW_LIVE_ORDERS" not in text or "=" not in text.split("ALLOW_LIVE_ORDERS")[1][:3]
