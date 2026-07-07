"""Phase 2 — HORIZON test. The research verdict: at 15m the bot is the slow uninformed side
paying spread to HFT; documented edge lives at LONGER horizons. Before building full 1h/4h
lane farms, cheaply answer: does the SAME method pool show better edge on 1h / 4h than 15m?

Backtests the whole method pool on each timeframe over a comparable bar-count window and reports
per-TF aggregate + best methods. Fixed sl/tp/timeout are NOT TF-tuned (a 1% stop means something
different per TF) — so treat this as a DIRECTIONAL signal, not a verdict: if a TF shows materially
better edge even with untuned exits, it's worth building properly. Read-only, paper. No API writes.
"""
from __future__ import annotations

import time

import method_lab as ml
import orderflow_data as of
from tradingagents.binance.client import spot_client

# (timeframe, fetch months) sized for ~1000-1700 bars each so warmup (i>=200) + sample are fair
TFS = [("15m", 0.5), ("1h", 2.0), ("4h", 6.0)]
UNIV_N = 12
MIN_QVOL = 50e6
MECH_LEV = 10
ATRM = 3.0
GRM = 1.5


def universe(c):
    ticks = c.futures_ticker()
    return [t["symbol"] for t in sorted(
        [x for x in ticks if x.get("symbol", "").endswith("USDT") and "_" not in x["symbol"]
         and float(x.get("quoteVolume", 0) or 0) >= MIN_QVOL],
        key=lambda x: -float(x.get("quoteVolume", 0) or 0))[:UNIV_N]]


def main():
    c = spot_client()
    now = int(time.time() * 1000)
    syms = universe(c)
    import json
    from pathlib import Path
    methods = []
    p = Path("state/method_lab/methods_pool.jsonl")
    for line in p.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                methods.append(json.loads(line))
            except Exception:
                pass
    print(f"universe={len(syms)} methods={len(methods)}\n")
    ld = 100.0 / max(1, MECH_LEV)

    for tf, months in TFS:
        frames = {}
        for s in syms:
            try:
                bars = [b for b in of.fetch_klines_with_flow(s, tf, months=months, end_ms=now,
                                                             client=c, sleep_between=0.03)
                        if b.get("is_final", True)]
                if len(bars) >= 250:
                    frames[s] = ml.feature_frame(bars)
            except Exception:
                continue
        # backtest every method across the universe, applying the SAME gap-gate as live
        stats = {}
        for m in methods:
            mid = m.get("id")
            if not mid:
                continue
            wins = n = 0
            net = 0.0
            for sym, rows in frames.items():
                # gate-filter rows first (mission parity): drop bars the executor would veto
                for t in ml.backtest_method(rows, m, sym):
                    n += 1
                    net += t["net"]
                    wins += t["net"] > 0
            if n >= 20:
                stats[mid] = (round(net / n * 100, 4), round(wins / n, 3), n)
        pos = [k for k, v in stats.items() if v[0] > 0]
        allm = list(stats)
        best = sorted(stats.items(), key=lambda kv: -kv[1][0])[:5]
        avg = round(sum(v[0] for v in stats.values()) / len(stats), 4) if stats else None
        tot_tr = sum(v[2] for v in stats.values())
        print(f"=== TF {tf} (window {months}mo, {len(frames)} coins, {tot_tr} trades) ===")
        print(f"  methods evaluable(n>=20): {len(allm)} | +EV: {len(pos)} ({100*len(pos)//max(1,len(allm))}%) | mean exp: {avg}%")
        for k, (e, w, n) in best:
            print(f"    {k:28s} exp={e:+.3f}% win={w:.0%} n={n}")
        # MOMENTUM family focus (Phase 2b hypothesis: trend edge lives at longer horizons)
        mo = {k: v for k, v in stats.items() if k.startswith("mo_")}
        if mo:
            mo_pos = [k for k, v in mo.items() if v[0] > 0]
            mo_avg = round(sum(v[0] for v in mo.values()) / len(mo), 4)
            mbest = sorted(mo.items(), key=lambda kv: -kv[1][0])[:3]
            print(f"  >> MOMENTUM: {len(mo)} eval | +EV {len(mo_pos)}/{len(mo)} | mean {mo_avg}% | "
                  + " ".join(f"{k}={v[0]:+.2f}%(n{v[2]})" for k, v in mbest))
        print()


if __name__ == "__main__":
    main()
