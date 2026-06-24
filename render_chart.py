"""Render Binance futures chart as PNG with indicators."""
import sys
import os
os.environ.setdefault("PYTHONIOENCODING", "utf-8")
from dotenv import load_dotenv
load_dotenv()
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import Rectangle
from datetime import datetime
from tradingagents.binance.client import spot_client


def ema(values, period):
    if len(values) < period:
        return [None] * len(values)
    out = [None] * (period - 1)
    sma = sum(values[:period]) / period
    out.append(sma)
    k = 2 / (period + 1)
    for v in values[period:]:
        sma = v * k + sma * (1 - k)
        out.append(sma)
    return out


def rsi(closes, period=14):
    if len(closes) < period + 1:
        return [None] * len(closes)
    out = [None] * period
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i-1]
        gains.append(max(d, 0))
        losses.append(max(-d, 0))
    for i in range(period, len(gains) + 1):
        ag = sum(gains[i-period:i]) / period
        al = sum(losses[i-period:i]) / period
        if al == 0:
            out.append(100)
        else:
            out.append(100 - 100 / (1 + ag / al))
    return out


def render(symbol, interval="1h", limit=72, save_path=None):
    c = spot_client()
    klines = c.futures_klines(symbol=symbol, interval=interval, limit=limit)
    if not klines:
        return None
    times = [datetime.fromtimestamp(int(k[0])/1000) for k in klines]
    opens = [float(k[1]) for k in klines]
    highs = [float(k[2]) for k in klines]
    lows = [float(k[3]) for k in klines]
    closes = [float(k[4]) for k in klines]
    vols = [float(k[7])/1e6 for k in klines]

    e20 = ema(closes, 20)
    e50 = ema(closes, 50)
    rsi_vals = rsi(closes)

    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(14, 10), sharex=True,
                                          gridspec_kw={"height_ratios": [3, 1, 1]})

    width = (mdates.date2num(times[1]) - mdates.date2num(times[0])) * 0.7
    for t, o, h, l, cl in zip(times, opens, highs, lows, closes):
        color = "#26a69a" if cl >= o else "#ef5350"
        x = mdates.date2num(t)
        ax1.plot([x, x], [l, h], color=color, linewidth=0.8, zorder=2)
        height = abs(cl - o)
        bottom = min(o, cl)
        if height < 1e-9:
            height = (h - l) * 0.05
        ax1.add_patch(Rectangle((x - width/2, bottom), width, height,
                                  facecolor=color, edgecolor=color, zorder=3))

    x_dates = [mdates.date2num(t) for t in times]
    ax1.plot(x_dates, e20, color="#f5a623", linewidth=1.5, label="EMA20", zorder=4)
    ax1.plot(x_dates, e50, color="#9013fe", linewidth=1.5, label="EMA50", zorder=4)
    ax1.legend(loc="upper left", fontsize=9)
    ax1.set_title(f"{symbol} {interval} (last {limit} candles) - current ${closes[-1]:.4f}",
                    fontsize=12, fontweight="bold")
    ax1.set_ylabel("Price USD")
    ax1.grid(True, alpha=0.3)

    vol_colors = ["#26a69a" if cl >= o else "#ef5350" for o, cl in zip(opens, closes)]
    ax2.bar(x_dates, vols, width=width, color=vol_colors, zorder=2)
    ax2.set_ylabel("Vol ($M)")
    ax2.grid(True, alpha=0.3)

    ax3.plot(x_dates, rsi_vals, color="#42a5f5", linewidth=1.5)
    ax3.axhline(70, color="red", linestyle="--", alpha=0.5)
    ax3.axhline(30, color="green", linestyle="--", alpha=0.5)
    ax3.axhline(50, color="gray", linestyle=":", alpha=0.5)
    ax3.set_ylim(0, 100)
    ax3.set_ylabel("RSI(14)")
    ax3.grid(True, alpha=0.3)

    ax3.xaxis.set_major_formatter(mdates.DateFormatter("%m-%d %H:%M"))
    plt.setp(ax3.xaxis.get_majorticklabels(), rotation=45, ha="right")
    plt.tight_layout()

    if save_path is None:
        save_path = f"state/chart_{symbol}_{interval}.png"
    os.makedirs(os.path.dirname(save_path), exist_ok=True)
    plt.savefig(save_path, dpi=100, bbox_inches="tight")
    plt.close()
    return save_path


if __name__ == "__main__":
    sym = sys.argv[1] if len(sys.argv) > 1 else "BTCUSDT"
    interval = sys.argv[2] if len(sys.argv) > 2 else "1h"
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else 72
    path = render(sym, interval, limit)
    print(f"Saved: {path}")
