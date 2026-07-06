"""Winrate mining on OUR OWN data (owner: 'cào thêm data nâng winrate').

For every historical capitulation fire (rsi14<22 & vol_ratio>1.8) on the liquid
universe, simulate the ARMED exits (SL1/TP6/TO48, purged tail) and record the
fire-bar features. Then report win-rate & meanR per feature bucket — the honest,
falsifiable version of 'what separates winners from losers'. Read-only research;
any promising filter still goes through the full validation pipeline before it
touches the bot. Paper/offline only.
"""
from __future__ import annotations

import json
import os
import time
from collections import defaultdict

os.environ.setdefault("INGEST_DECISION_CANDLES", "0")

import method_lab as ml
import orderflow_data as of

SL, TP, TO = 1.0, 6.0, 48
FEE = ml.FEE_RT
MIN_QVOL = 50e6


def outcome(rows, i):
    entry = rows[i]["close"]
    sl, tp = entry * (1 - SL / 100), entry * (1 + TP / 100)
    n = len(rows)
    if i + TO >= n:                       # purged tail
        return None
    for j in range(i + 1, i + 1 + TO):
        if rows[j]["low"] <= sl:
            return (sl / entry - 1) - FEE
        if rows[j]["high"] >= tp:
            return (tp / entry - 1) - FEE
    return (rows[i + TO]["close"] / entry - 1) - FEE


def bucket(feats):
    b = {}
    b["ema4h"] = {1.0: "4h_BULL", -1.0: "4h_BEAR"}.get(feats.get("ema4h_state"), "4h_?")
    fz = feats.get("funding_z") or 0
    b["fundz"] = "fz<-1" if fz < -1 else ("fz>1" if fz > 1 else "fz~0")
    dd = feats.get("dd96_pct") or 0
    b["dd96"] = "dd<5" if dd < 5 else ("dd5-12" if dd < 12 else "dd>12")
    b["cpos"] = "close_lo" if (feats.get("close_pos") or 0) < 0.35 else (
        "close_hi" if (feats.get("close_pos") or 0) > 0.65 else "close_mid")
    bz = feats.get("bar_z") or 0
    b["barz"] = "bz<-3" if bz < -3 else ("bz-3..-1.5" if bz < -1.5 else "bz>-1.5")
    vr = feats.get("vol_ratio") or 0
    b["vol"] = "vol1.8-3" if vr < 3 else ("vol3-6" if vr < 6 else "vol>6")
    sd = feats.get("streak_down") or 0
    b["streak"] = "sd<=2" if sd <= 2 else ("sd3-5" if sd <= 5 else "sd>5")
    h = int(feats.get("hour_utc") or 0)
    b["sess"] = "asia" if 0 <= h < 8 else ("eu" if h < 16 else "us")
    b["ema200"] = "abv200" if (feats.get("px_vs_ema200") or 0) > 0 else "und200"
    rsi = feats.get("rsi14") or 0
    b["rsi"] = "rsi<15" if rsi < 15 else "rsi15-22"
    return b


def main():
    from tradingagents.binance.client import spot_client
    cli = spot_client()
    now = int(time.time() * 1000)
    ticks = cli.futures_ticker()
    uni = sorted(t["symbol"] for t in ticks
                 if t.get("symbol", "").endswith("USDT") and "_" not in t["symbol"]
                 and float(t.get("quoteVolume", 0) or 0) >= MIN_QVOL)
    fires = []
    for sym in uni:
        try:
            bars = of.fetch_klines_with_flow(sym, "15m", months=5.0, end_ms=now,
                                             client=cli, sleep_between=0.02)
            try:
                fund = of.fetch_funding_series(sym, months=5.0, end_ms=now, client=cli)
            except Exception:
                fund = None
            rows = ml.feature_frame(bars, funding=fund)
            if len(rows) < 2000:
                continue
            i = 200
            while i < len(rows) - 1:
                r = rows[i]
                if (r.get("rsi14") or 99) < 22 and (r.get("vol_ratio") or 0) > 1.8:
                    net = outcome(rows, i)
                    if net is not None:
                        fires.append({"sym": sym, "net": net, "win": net > 0, "b": bucket(r)})
                        i += TO                       # no overlap, mirror engine
                        continue
                i += 1
        except Exception:
            continue

    n = len(fires)
    wins = sum(1 for f in fires if f["win"])
    base_wr = wins / n if n else 0
    base_mr = sum(f["net"] for f in fires) / n * 100 if n else 0
    out = {"n_fires": n, "base_winrate": round(base_wr, 4),
           "base_mean_net_pct": round(base_mr, 4), "buckets": {}}
    for dim in ["ema4h", "fundz", "dd96", "cpos", "barz", "vol", "streak", "sess", "ema200", "rsi"]:
        agg = defaultdict(lambda: [0, 0, 0.0])
        for f in fires:
            k = f["b"][dim]
            agg[k][0] += 1
            agg[k][1] += 1 if f["win"] else 0
            agg[k][2] += f["net"]
        out["buckets"][dim] = {k: {"n": v[0], "wr": round(v[1] / v[0], 3),
                                   "mean_net_pct": round(v[2] / v[0] * 100, 3)}
                               for k, v in sorted(agg.items()) if v[0] >= 25}
    with open("state/memory/winrate_mining.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1)
    print(json.dumps({"n_fires": n, "base_wr": out["base_winrate"],
                      "base_mean_net_pct": out["base_mean_net_pct"]}))


if __name__ == "__main__":
    main()
