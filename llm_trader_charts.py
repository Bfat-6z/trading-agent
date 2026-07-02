"""Render a candlestick + EMA + volume chart to a base64 PNG so a vision LLM can
literally SEE the chart (the way a discretionary trader does), instead of trading
off a handful of scalar numbers.

Pure + offline: matplotlib Agg only, no network, no file writes. Bars are the
plain dicts returned by orderflow_data.fetch_klines_with_flow
(open/high/low/close/volume/ts_ms). Robust to short series and missing keys.
"""
from __future__ import annotations

import base64
import io
from typing import Any, Sequence

import matplotlib
matplotlib.use("Agg")  # headless — no display needed
import matplotlib.pyplot as plt
import numpy as np


def _ema(values: np.ndarray, period: int) -> np.ndarray:
    """Standard EMA; seeded with the first value. Returns same length as input."""
    if len(values) == 0:
        return values
    alpha = 2.0 / (period + 1.0)
    out = np.empty_like(values, dtype=float)
    out[0] = values[0]
    for i in range(1, len(values)):
        out[i] = alpha * values[i] + (1 - alpha) * out[i - 1]
    return out


def _f(bar: dict[str, Any], *keys: str) -> float:
    for k in keys:
        v = bar.get(k)
        if v is not None:
            try:
                return float(v)
            except Exception:
                pass
    return 0.0


def render_chart(symbol: str, bars: Sequence[dict[str, Any]], *,
                 tf: str = "15m", lookback: int = 64,
                 ema_periods: tuple[int, ...] = (20, 50, 200)) -> str | None:
    """Render the last `lookback` closed candles with EMAs + a volume panel.
    Returns a base64 PNG string (no data-url prefix), or None if unrenderable.

    Candles: up (close>=open) drawn hollow/white, down drawn solid black — a
    monochrome scheme that stays legible to the model. EMAs are computed over the
    FULL series passed in (so a 200-EMA is meaningful) then the view is cropped to
    the last `lookback` bars.
    """
    if not bars or len(bars) < 5:
        return None
    o = np.array([_f(b, "open") for b in bars], dtype=float)
    h = np.array([_f(b, "high") for b in bars], dtype=float)
    l = np.array([_f(b, "low") for b in bars], dtype=float)
    c = np.array([_f(b, "close") for b in bars], dtype=float)
    v = np.array([_f(b, "volume", "quote_volume") for b in bars], dtype=float)
    if not np.all(np.isfinite(c)) or c.max() <= 0:
        return None
    emas = {p: _ema(c, p) for p in ema_periods}

    n = len(c)
    start = max(0, n - lookback)
    idx = np.arange(start, n)
    xr = np.arange(len(idx))  # 0..view-1 for compact x

    try:
        fig, (ax, axv) = plt.subplots(
            2, 1, figsize=(7.2, 4.6), dpi=100, sharex=True,
            gridspec_kw={"height_ratios": [3.2, 1], "hspace": 0.05})
        fig.patch.set_facecolor("white")
        # --- candles ---
        width = 0.62
        for xi, bi in zip(xr, idx):
            up = c[bi] >= o[bi]
            ax.vlines(xi, l[bi], h[bi], color="black", linewidth=0.8, zorder=2)
            lo, hi = (o[bi], c[bi]) if up else (c[bi], o[bi])
            body = max(hi - lo, (c[bi] * 1e-5))  # avoid zero-height
            ax.add_patch(plt.Rectangle((xi - width / 2, lo), width, body,
                                       facecolor=("white" if up else "black"),
                                       edgecolor="black", linewidth=0.8, zorder=3))
        # --- EMAs (cropped to view) ---
        styles = {20: ("-", 1.2, "#111"), 50: ("-", 1.4, "#888"), 200: ("--", 1.4, "#444")}
        for p, series in emas.items():
            st, lw, col = styles.get(p, ("-", 1.0, "#555"))
            ax.plot(xr, series[idx], st, linewidth=lw, color=col,
                    label=f"EMA{p}", zorder=4)
        last = c[-1]
        ax.axhline(last, color="black", linewidth=0.6, linestyle=":", alpha=0.5)
        ax.annotate(f"{last:.4g}", xy=(xr[-1], last), xytext=(4, 0),
                    textcoords="offset points", va="center", fontsize=8, fontweight="bold")
        ax.set_title(f"{symbol} · {tf} · last {len(idx)} closed bars", fontsize=11, fontweight="bold")
        ax.legend(loc="upper left", fontsize=7, framealpha=0.6)
        ax.grid(True, alpha=0.15, linewidth=0.5)
        ax.margins(x=0.02)
        # --- volume ---
        vcolors = ["#bbbbbb" if c[bi] >= o[bi] else "#333333" for bi in idx]
        axv.bar(xr, v[idx], width=0.7, color=vcolors)
        vma = np.convolve(v, np.ones(20) / 20, mode="same") if n >= 20 else v
        axv.plot(xr, vma[idx], color="black", linewidth=1.0, alpha=0.7)
        axv.set_ylabel("vol", fontsize=7)
        axv.grid(True, alpha=0.15, linewidth=0.5)
        axv.tick_params(labelsize=6)
        ax.tick_params(labelbottom=False, labelsize=6)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight")
        plt.close(fig)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        try:
            plt.close("all")
        except Exception:
            pass
        return None
