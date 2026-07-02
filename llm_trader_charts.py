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


def _swings(h: np.ndarray, l: np.ndarray, k: int = 3):
    """Fractal swing highs/lows: a bar that is the max/min of its +/-k neighbours.
    Returns (highs, lows) as lists of (index, price)."""
    highs, lows = [], []
    for i in range(k, len(h) - k):
        if h[i] >= h[i - k:i + k + 1].max():
            highs.append((i, float(h[i])))
        if l[i] <= l[i - k:i + k + 1].min():
            lows.append((i, float(l[i])))
    return highs, lows


def _fvgs(o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray, max_keep: int = 4):
    """Fair-Value-Gap / imbalance zones (3-candle). Bullish: low[i+1] > high[i-1]
    (price gapped up, empty zone below); bearish: high[i+1] < low[i-1]. Keeps the
    most recent gaps NOT yet fully closed by later price. Returns
    [(gap_bar_idx, bottom, top, kind)]."""
    n = len(c)
    out = []
    for i in range(1, n - 1):
        if l[i + 1] > h[i - 1]:
            bot, top, kind = float(h[i - 1]), float(l[i + 1]), "bull"
        elif h[i + 1] < l[i - 1]:
            bot, top, kind = float(h[i + 1]), float(l[i - 1]), "bear"
        else:
            continue
        # unfilled = later price hasn't traded fully back through the far edge
        later_lo = l[i + 2:].min() if n > i + 2 else float("inf")
        later_hi = h[i + 2:].max() if n > i + 2 else float("-inf")
        filled = (later_lo <= bot) if kind == "bull" else (later_hi >= top)
        if not filled:
            out.append((i, bot, top, kind))
    return out[-max_keep:]


def render_chart(symbol: str, bars: Sequence[dict[str, Any]], *,
                 tf: str = "15m", lookback: int = 64,
                 ema_periods: tuple[int, ...] = (20, 50, 200),
                 hlines: Sequence[tuple[float, str, str]] | None = None,
                 title_suffix: str = "") -> str | None:
    """Dark TradingView-style chart: green/red candlesticks, EMA20/50/200, marked
    FVG / imbalance zones, recent swing support/resistance levels, and a volume
    panel. Returns a base64 PNG (no data-url prefix) or None.

    EMAs are computed over the FULL series (so EMA200 is meaningful) then cropped
    to the last `lookback` bars. hlines = optional [(price,label,color)] reference
    lines (entry/SL/TP); off-screen ones are skipped.
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
    xr = np.arange(len(idx))
    x_of = {int(bi): xi for xi, bi in zip(xr, idx)}   # global bar -> view x

    # dark palette (TradingView-ish, tinted — matches the dashboard)
    BG, GRID, TXT = "#131722", "#2a2e39", "#d1d4dc"
    UP, DN = "#26a69a", "#ef5350"
    BULL_Z, BEAR_Z = "#26a69a", "#ef5350"

    try:
        fig, (ax, axv) = plt.subplots(
            2, 1, figsize=(7.4, 4.7), dpi=100, sharex=True,
            gridspec_kw={"height_ratios": [3.3, 1], "hspace": 0.06})
        fig.patch.set_facecolor(BG)
        for a in (ax, axv):
            a.set_facecolor(BG)
            a.grid(True, color=GRID, alpha=0.5, linewidth=0.5)
            a.tick_params(colors=TXT, labelsize=6)
            for s in a.spines.values():
                s.set_color(GRID)

        # --- FVG / imbalance zones (draw first, behind candles) ---
        rmax = xr[-1] + 0.6
        for gi, bot, top, kind in _fvgs(o, h, l, c):
            if gi < start:
                continue
            x0 = x_of.get(int(gi), 0) - 0.5
            col = BULL_Z if kind == "bull" else BEAR_Z
            ax.add_patch(plt.Rectangle((x0, bot), rmax - x0, top - bot,
                                       facecolor=col, alpha=0.11, edgecolor=col,
                                       linewidth=0.6, zorder=1))
            ax.annotate("imbalance", xy=(x0 + 0.6, (bot + top) / 2), fontsize=6.5,
                        color=col, va="center", alpha=0.85, zorder=6)

        # --- candles (green up / red down) ---
        width = 0.66
        for xi, bi in zip(xr, idx):
            up = c[bi] >= o[bi]
            col = UP if up else DN
            ax.vlines(xi, l[bi], h[bi], color=col, linewidth=0.9, zorder=2)
            lo, hi = (o[bi], c[bi]) if up else (c[bi], o[bi])
            ax.add_patch(plt.Rectangle((xi - width / 2, lo), width, max(hi - lo, c[bi] * 1e-5),
                                       facecolor=col, edgecolor=col, linewidth=0.8, zorder=3))

        # --- EMAs ---
        estyles = {20: ("-", 1.2, "#e6e6e6"), 50: ("-", 1.4, "#f0b90b"), 200: ("--", 1.3, "#787b86")}
        for p, series in emas.items():
            st, lw, col = estyles.get(p, ("-", 1.0, "#9aa0aa"))
            ax.plot(xr, series[idx], st, linewidth=lw, color=col, label=f"EMA{p}", zorder=4)

        # --- swing support/resistance (most recent high + low in view) ---
        hs, ls = _swings(h[idx], l[idx])
        for lvl_list, lab, col in ((hs, "R", "#8a8f99"), (ls, "S", "#8a8f99")):
            if lvl_list:
                _, price = lvl_list[-1]
                ax.axhline(price, color=col, linewidth=0.8, linestyle=(0, (4, 3)), alpha=0.55, zorder=2)
                ax.annotate(f"{lab} {price:.4g}", xy=(rmax, price), xytext=(-2, 2),
                            textcoords="offset points", ha="right", fontsize=6.5, color=col, alpha=0.8)

        # --- last price ---
        last = c[-1]
        ax.axhline(last, color=TXT, linewidth=0.6, linestyle=":", alpha=0.45, zorder=2)
        ax.annotate(f"{last:.4g}", xy=(xr[-1], last), xytext=(5, 0), textcoords="offset points",
                    va="center", fontsize=8, fontweight="bold", color=TXT,
                    bbox=dict(boxstyle="round,pad=0.2", fc=BG, ec=GRID, lw=0.6))

        # --- reference lines (entry/SL/TP) ---
        if hlines:
            view = c[idx]; vlo, vhi = float(view.min()), float(view.max())
            band = max((vhi - vlo) * 0.6, vhi * 0.01)
            for price, label, color in hlines:
                try:
                    price = float(price)
                except Exception:
                    continue
                if not (vlo - band <= price <= vhi + band):
                    continue
                ax.axhline(price, color=color, linewidth=1.3, linestyle="--", alpha=0.95, zorder=5)
                ax.annotate(f"{label} {price:.4g}", xy=(0, price), xytext=(2, 2),
                            textcoords="offset points", fontsize=7.5, fontweight="bold", color=color, va="bottom")

        ax.set_title(f"{symbol} · {tf} · last {len(idx)} closed bars{title_suffix}",
                     fontsize=11, fontweight="bold", color=TXT)
        leg = ax.legend(loc="upper left", fontsize=7, framealpha=0.25, facecolor=BG, edgecolor=GRID)
        for t in leg.get_texts():
            t.set_color(TXT)
        ax.margins(x=0.02)

        # --- volume ---
        vcolors = [UP if c[bi] >= o[bi] else DN for bi in idx]
        axv.bar(xr, v[idx], width=0.72, color=vcolors, alpha=0.65)
        vma = np.convolve(v, np.ones(20) / 20, mode="same") if n >= 20 else v
        axv.plot(xr, vma[idx], color="#f0b90b", linewidth=1.0, alpha=0.8)
        axv.set_ylabel("vol", fontsize=7, color=TXT)
        ax.tick_params(labelbottom=False)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", bbox_inches="tight", facecolor=BG)
        plt.close(fig)
        return base64.b64encode(buf.getvalue()).decode("ascii")
    except Exception:
        try:
            plt.close("all")
        except Exception:
            pass
        return None
