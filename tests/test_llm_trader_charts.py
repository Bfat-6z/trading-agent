"""llm_trader_charts.render_chart — must produce a valid PNG the vision LLM can
read, and degrade safely on bad input (never raise into the trading loop)."""
from __future__ import annotations

import base64
import io
import math

import llm_trader_charts as ch


def _bars(n=120, start=100.0):
    bars, px = [], start
    for k in range(n):
        o = px
        px = px * (1 + (0.01 if k % 3 == 0 else -0.006))  # deterministic wiggle
        hi = max(o, px) * 1.003
        lo = min(o, px) * 0.997
        bars.append({"open": o, "high": hi, "low": lo, "close": px,
                     "volume": 1000 + (k % 7) * 50, "ts_ms": 1_700_000_000_000 + k * 900_000})
    return bars


def test_renders_valid_png():
    b64 = ch.render_chart("BTCUSDT", _bars(140), tf="15m")
    assert isinstance(b64, str) and len(b64) > 1000
    raw = base64.b64decode(b64)
    assert raw[:8] == b"\x89PNG\r\n\x1a\n", "output is a real PNG"


def test_short_series_still_ok_or_none():
    # >=5 bars renders; <5 returns None (no raise)
    assert ch.render_chart("X", _bars(6)) is not None
    assert ch.render_chart("X", _bars(3)) is None
    assert ch.render_chart("X", []) is None


def test_missing_keys_do_not_raise():
    bars = [{"close": 100 + i} for i in range(30)]  # only close present
    out = ch.render_chart("X", bars)
    assert out is None or isinstance(out, str)  # must not raise


def test_hlines_render_without_error():
    # entry/SL/TP reference lines must render (in-band) and never raise
    bars = _bars(120, start=100.0)
    out = ch.render_chart("BTCUSDT", bars, tf="15m",
                          hlines=[(bars[-1]["close"], "ENTRY", "#c99a00"),
                                  (bars[-1]["close"] * 0.98, "SL", "#d43a4b"),
                                  (bars[-1]["close"] * 1.02, "TP", "#0a9d66"),
                                  (bars[-1]["close"] * 0.5, "LIQ-far", "#f00")],  # off-screen -> skipped
                          title_suffix=" · LIVE POSITION")
    assert isinstance(out, str) and base64.b64decode(out)[:8] == b"\x89PNG\r\n\x1a\n"


def test_ema_matches_reference():
    import numpy as np
    vals = np.array([10.0, 11, 12, 11, 13, 14, 13, 15], dtype=float)
    got = ch._ema(vals, 3)
    # reference EMA seeded with first value, alpha=2/(3+1)=0.5
    a = 0.5
    ref = [vals[0]]
    for x in vals[1:]:
        ref.append(a * x + (1 - a) * ref[-1])
    assert all(math.isclose(g, r, rel_tol=1e-9) for g, r in zip(got, ref))
