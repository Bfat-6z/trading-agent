"""Render charts cho positions live + key candidates."""
from dotenv import load_dotenv
load_dotenv()
from tradingagents.binance.client import spot_client
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.dates import DateFormatter
from datetime import datetime
import os

c = spot_client()

# Position info to overlay
POS = {
    "SUIUSDT": {"entry": 1.0684, "sl": 1.0576, "tp": 1.090, "side": "LONG"},
    "TRXUSDT": {"entry": 0.36345, "sl": 0.3608, "tp": 0.3678, "side": "LONG"},
    "HYPEUSDT": {"entry": None, "sl": None, "tp": None, "side": "WATCH"},  # Watch only
}

def fetch(sym, interval, limit=80):
    k = c.futures_klines(symbol=sym, interval=interval, limit=limit)
    return [{
        "t": datetime.utcfromtimestamp(int(x[0])/1000),
        "o": float(x[1]), "h": float(x[2]), "l": float(x[3]), "c": float(x[4]),
        "v": float(x[5])
    } for x in k]

def ema(values, period):
    if not values: return []
    k = 2 / (period + 1)
    result = [values[0]]
    for v in values[1:]:
        result.append(v * k + result[-1] * (1-k))
    return result

def rsi(values, period=14):
    if len(values) < period+1: return [50]*len(values)
    gains, losses = [], []
    for i in range(1, len(values)):
        d = values[i] - values[i-1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    avg_g = sum(gains[:period])/period
    avg_l = sum(losses[:period])/period
    rsis = [50]*period
    for i in range(period, len(gains)):
        avg_g = (avg_g*(period-1) + gains[i]) / period
        avg_l = (avg_l*(period-1) + losses[i]) / period
        rs = avg_g / avg_l if avg_l > 0 else 99
        rsis.append(100 - 100/(1+rs))
    rsis.append(rsis[-1])
    return rsis

def render(sym, save_path, pos_info=None):
    fig, axes = plt.subplots(2, 2, figsize=(18, 11))
    for col, (interval, label, w) in enumerate([("15m", "15m", 0.008), ("1h", "1h", 0.03)]):
        k = fetch(sym, interval, 80)
        ax_p = axes[0, col]; ax_v = axes[1, col]
        closes = [b["c"] for b in k]; times = [b["t"] for b in k]
        ema20 = ema(closes, 20); ema50 = ema(closes, 50)
        rsis = rsi(closes, 14)

        for i, b in enumerate(k):
            color = "g" if b["c"] >= b["o"] else "r"
            ax_p.plot([times[i], times[i]], [b["l"], b["h"]], color=color, linewidth=0.8)
            ax_p.bar(times[i], b["c"]-b["o"], width=w, bottom=b["o"], color=color, alpha=0.7)

        ax_p.plot(times, ema20, color="blue", linewidth=1.2, label="EMA20")
        ax_p.plot(times, ema50, color="orange", linewidth=1.2, label="EMA50")

        # Overlay position lines
        if pos_info and pos_info.get("entry"):
            ax_p.axhline(pos_info["entry"], color="black", linestyle="-", linewidth=1.5, alpha=0.6, label=f"Entry ${pos_info['entry']}")
            ax_p.axhline(pos_info["sl"], color="red", linestyle="--", linewidth=1.5, label=f"SL ${pos_info['sl']}")
            ax_p.axhline(pos_info["tp"], color="green", linestyle="--", linewidth=1.5, label=f"TP ${pos_info['tp']}")

        title = f"{sym} {label} | Close: ${closes[-1]:.5f} | RSI14: {rsis[-1]:.1f}"
        if pos_info and pos_info.get("entry"):
            unr_pct = (closes[-1]/pos_info["entry"] - 1) * 100
            title += f" | {pos_info['side']} {unr_pct:+.2f}%"
        ax_p.set_title(title, fontsize=11)
        ax_p.legend(loc="upper left", fontsize=8)
        ax_p.grid(alpha=0.3)
        ax_p.xaxis.set_major_formatter(DateFormatter("%m-%d %H:%M"))
        plt.setp(ax_p.xaxis.get_majorticklabels(), rotation=45, fontsize=8)

        vols = [b["v"] for b in k]
        colors = ["g" if b["c"]>=b["o"] else "r" for b in k]
        ax_v.bar(times, vols, color=colors, alpha=0.6, width=w)
        ax_v.set_ylabel("Volume"); ax_v.grid(alpha=0.3)
        ax_v.xaxis.set_major_formatter(DateFormatter("%m-%d %H:%M"))
        plt.setp(ax_v.xaxis.get_majorticklabels(), rotation=45, fontsize=8)

        ax_rsi = ax_v.twinx()
        ax_rsi.plot(times, rsis, color="purple", linewidth=1.5)
        ax_rsi.axhline(70, color="red", linestyle="--", alpha=0.5)
        ax_rsi.axhline(30, color="green", linestyle="--", alpha=0.5)
        ax_rsi.axhline(50, color="gray", linestyle=":", alpha=0.4)
        ax_rsi.set_ylabel("RSI", color="purple"); ax_rsi.set_ylim(0, 100)

    plt.tight_layout()
    os.makedirs("charts", exist_ok=True)
    plt.savefig(save_path, dpi=80, bbox_inches="tight")
    plt.close()
    print(f"Saved {save_path}")

for sym, info in POS.items():
    try:
        render(sym, f"charts/{sym}_pos.png", info)
    except Exception as e:
        print(f"{sym}: {e}")
